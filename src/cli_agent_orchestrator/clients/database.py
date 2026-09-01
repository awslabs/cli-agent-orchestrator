"""Minimal database client with only terminal metadata."""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, cast

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    literal_column,
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
    provider = Column(String, nullable=False)  # "kiro_cli", "claude_code"
    agent_profile = Column(String)  # "developer", "reviewer" (optional)
    working_directory = Column(String, nullable=True)  # launch-time cwd (optional)
    allowed_tools = Column(String, nullable=True)  # JSON-encoded list of CAO tool names
    shell_command = Column(String, nullable=True)  # shell process name captured before kiro launch
    caller_id = Column(String, nullable=True)  # terminal that created this one (callback target)
    engine = Column(String, nullable=True)  # resolved Kiro engine; NULL for legacy/non-Kiro rows
    # Ordered, general-to-specific array of strings (JSON-encoded), e.g.
    # '["tenant_1", "project_5", "folder_12"]'. CAO only does ordered-prefix
    # matching (list_siblings); consumers own what the levels mean (#432).
    group = Column(Text, nullable=True)
    # Free-form JSON (JSON-encoded dict), consumer-defined, no fixed schema.
    # Python attribute is ``metadata_json`` (not ``metadata``) because
    # SQLAlchemy's declarative Base reserves ``.metadata`` for the schema
    # MetaData object on every mapped class; the DB column itself is still
    # literally named "metadata" per #432's design.
    metadata_json = Column("metadata", Text, nullable=True)
    last_active = Column(DateTime, default=datetime.now)

    # ORDERING CONTRACT: the two session-scoped reads -- ``list_terminals_by_session``
    # and ``list_terminals_in_sessions`` -- order by SQLite's implicit ``rowid``,
    # so index 0 of a session's terminals is its OLDEST SURVIVING row.
    # (``list_siblings_by_group_prefix`` also reads this table by session and is
    # deliberately NOT ordered: its consumer matches on group prefixes and does
    # not take a first element. Order it too if that ever changes.) Several consumers
    # treat that as the session's conductor and one of them kills sessions on it.
    # rowid is not declared above, so the dependency is invisible here. Three
    # things break it SILENTLY:
    #   * an ``INSERT OR REPLACE``/upsert against terminals -- a replace deletes
    #     and re-inserts, so the row gets a NEW rowid and jumps to the end of its
    #     session. Nothing does this today and
    #     ``test_no_upsert_against_the_terminals_table`` fails the build if it
    #     starts; use an UPDATE.
    #   * writing ``rowid`` explicitly (``INSERT (rowid, ...) VALUES (-5, ...)``).
    #   * giving ``id`` INTEGER affinity, which makes rowid an alias for it and
    #     hands the order back to a random uuid.
    # Declaring the table WITHOUT ROWID also breaks it but fails LOUDLY
    # (``no such column: terminals.rowid``), as does reading it through
    # ``aliased()`` or ``union()``.
    #
    # New rows sort after existing ones because SQLite assigns ``max(rowid)+1``
    # among surviving rows -- so deleting rows recycles values but cannot reorder
    # the ones still present. The exception is a table holding a row at
    # 2**63-1, where SQLite picks random unused rowids and within-session order
    # scrambles; unreachable without an explicit rowid write.
    #
    # Deliberately NOT a ``created_at`` column: rowid already records insertion
    # order exactly and correctly for every row in every existing database,
    # whereas a new column has to invent the value for rows that predate it, and
    # there is no honest source for it -- ``last_active`` is written only on
    # input delivery (send_input/send_special_key), so it is LATEST for the
    # busiest terminal, which is usually the conductor, and backfilling from it
    # inverts the very order this contract exists to preserve.
    #
    # Deliberately NOT ``caller_id`` either, and this one was priced rather than
    # dismissed. A conductor created through ``create_session`` records no
    # caller while an MCP-spawned worker records its supervisor, so
    # ``caller_id IS NOT NULL`` looks like root-terminal identity. On its own it
    # is not total -- ``caller_id`` is an optional parameter of the agent-step
    # create path, so a root carrying an explicit caller would be outranked by a
    # later terminal without one. It CAN be made total by also requiring the
    # parent to be in the same session (a correlated ``EXISTS`` on
    # ``tmux_session``, which is immutable after insert, so a root's caller can
    # never be in its own session). Measured, that costs ~+29% on this read
    # (50 sessions over 5k rows: 9.0ms -> 11.6ms) and couples the conductor pick
    # to referential integrity nothing enforces -- there is no FK on
    # ``caller_id`` and ghost terminals are deleted in three places, so a
    # deleted root leaves every worker's caller dangling. Rejected on that cost
    # and coupling, for a hazard the upsert guard above already turns into a red
    # build. ``caller_id`` is still the right thing to read for a specific
    # terminal's spawn parent; it is not worth its price as a sort key.


class InboxModel(Base):
    """SQLAlchemy model for inbox messages."""

    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sender_id = Column(String, nullable=False)
    receiver_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False)  # MessageStatus enum value
    created_at = Column(DateTime, default=datetime.now)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryMetadataModel(Base):
    """SQLAlchemy model for memory metadata (Phase 2 U1).

    SQLite is the source of truth for metadata queries; wiki markdown
    files remain the content store. Each row corresponds to exactly one
    wiki file on disk.
    """

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
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    # 3-factor scoring. ``access_count`` feeds the usage factor;
    # ``last_accessed_at`` backs a server-side rate-limit on increments. NOT
    # NULL DEFAULT 0 so existing rows read as "never recalled" without a
    # backfill. Migrated onto existing DBs by ``_migrate_add_access_count``.
    access_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_accessed_at = Column(DateTime(timezone=True), nullable=True, default=None)
    # LLM wiki compilation. NULL = never LLM-compiled (pre-existing rows, or
    # every compile attempt fell back to append). Non-NULL = UTC timestamp of
    # the last successful compile.
    last_compiled_at = Column(DateTime(timezone=True), nullable=True, default=None)
    # Comma-separated sanitised keys of cross-referenced articles. NULL =
    # never computed (pre-existing rows or LLM error). ``""`` = computed, no
    # related found (success — distinct from NULL to avoid endless retries).
    # Practical max ≤ 256 bytes (3 keys × 60 chars + 2 commas). The CHECK
    # constraint applies on FRESH databases only — existing DBs rely on the
    # parse-side cap in ``_parse_related_keys``.
    related_keys = Column(Text, nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("key", "scope", "scope_id", name="uq_memory_key_scope"),
        CheckConstraint(
            "related_keys IS NULL OR length(related_keys) < 1024",
            name="ck_related_keys_length",
        ),
    )


# Relationship-store sentinel: ``memory_relationships.scope_id`` is NOT NULL and
# stores this value for global/federated scope. SQLite treats ``NULL != NULL``
# in a UNIQUE index, so a nullable scope_id would make the dedup index (and thus
# ``INSERT ... ON CONFLICT``) inert for global scope — silently duplicating
# exactly the edges hardest to notice. Storing a NOT-NULL sentinel keeps the
# dedup tuple total. ``""`` cannot collide with a real sanitized scope_id
# (``MemoryService._sanitize_key``/``_sanitize_scope_id`` never yield empty —
# the latter returns ``"unknown"``). This sentinel is scoped to the
# ``memory_relationships`` table ONLY; ``MemoryMetadataModel.scope_id`` remains
# genuinely nullable (stores real NULL for global), so cross-table endpoint
# checks against it use logical ``None`` + ``.is_(None)`` (see the relationship
# service), never this sentinel.
RELATIONSHIP_SCOPE_ID_SENTINEL = ""


class MemoryRelationshipModel(Base):
    """SQLAlchemy model for a typed, durable memory relationship edge (issue #511).

    The authoritative relationship store that replaces the lossy
    ``memory_metadata.related_keys`` text column. One row per typed edge between
    two memory keys in the same ``(scope, scope_id)``. Written and read ONLY
    through ``MemoryRelationshipService`` — no other component issues SQL against
    this table (FR-2.1 single-boundary invariant).

    ``related_keys`` on ``MemoryMetadataModel`` is retained UNCHANGED as the
    compiler's computation-state marker (NULL = never computed/error, ``""`` =
    computed-empty) and is NOT modified or retired by this table (retirement is a
    separate, later change gated on a loss-free proof).
    """

    __tablename__ = "memory_relationships"

    # Application-generated uuid4 string PK, matching ``MemoryMetadataModel.id``
    # (str(uuid4())). API-stable identifier exposed in mutation responses.
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    scope = Column(String, nullable=False)
    # NOT NULL: sentinel RELATIONSHIP_SCOPE_ID_SENTINEL ("") for global/federated
    # so the dedup UNIQUE index is total (see the sentinel comment above).
    scope_id = Column(String, nullable=False)
    source_key = Column(String, nullable=False)
    target_key = Column(String, nullable=False)
    # Closed taxonomy reusing the graph EdgeType values.
    type = Column(String, nullable=False)  # relates_to | contradiction | supersedes
    # compiler | wiki_lint | human | legacy_related_keys | external_import(reserved)
    origin = Column(String, nullable=False)
    # active | proposal | rejected | superseded | deleted (auditable soft-delete)
    status = Column(String, nullable=False, default="active")
    # Optional evidence metadata. NULL = no evidence (NEVER fabricated / coerced
    # to 0); a stored value is a validated REAL in [0, 1].
    confidence = Column(Float, nullable=True, default=None)
    # Optional ordering hint (e.g. legacy related_keys position). NULL if none.
    rank = Column(Integer, nullable=True, default=None)
    # Bounded JSON blob. NULL if none; the CHECK caps FRESH DBs, the service
    # caps existing DBs (mirrors the ck_related_keys_length precedent).
    attributes_json = Column(Text, nullable=True, default=None)
    # The source memory's updated_at at write time; basis for staleness
    # detection (an edge is stale when this predates the source's current
    # updated_at). NULL when unknown.
    source_updated_at = Column(DateTime(timezone=True), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        # Dedup: differing type or origin coexist as distinct rows (multi-edge +
        # provenance-aware coexistence); a repeat of the same tuple upserts.
        # Every column is non-NULL (scope_id sentinel), so the index and
        # ON CONFLICT fire for ALL scopes including global.
        UniqueConstraint(
            "scope",
            "scope_id",
            "source_key",
            "target_key",
            "type",
            "origin",
            name="uq_memory_rel",
        ),
        # FRESH-DB CHECKs only (SQLite cannot retro-add a CHECK); the service
        # validates confidence range and attributes size on existing DBs.
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_memory_rel_confidence_range",
        ),
        CheckConstraint(
            "attributes_json IS NULL OR length(attributes_json) <= 2048",
            name="ck_memory_rel_attributes_size",
        ),
    )


class ProjectAliasModel(Base):
    """SQLAlchemy model for project identity aliases (Phase 2.5 U6).

    Maps historical/alternate project identifiers (cwd hashes, manual labels)
    to a canonical ``project_id`` so memory recall survives directory rename
    and worktree layouts.
    """

    __tablename__ = "project_aliases"

    # ``alias`` is the sole primary key: an alias maps to exactly one canonical
    # project_id, so reverse lookups (get_project_id_by_alias) are stable. A
    # cwd-hash first resolved via an override and later via its git remote
    # upserts the same row rather than creating a second, ambiguous mapping.
    alias = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)  # "git_remote" | "cwd_hash" | "manual"
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class WorkflowOutcomeModel(Base):
    """SQLAlchemy model for workflow outcome records (self-learning Phase 1).

    One row per reported outcome of a unit of agent work (a workflow step,
    a package conversion, a review round). Outcomes are the raw signal the
    retrospector agent distills into memory lessons — they carry short
    labels and notes, never transcripts or file contents.
    """

    __tablename__ = "workflow_outcomes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_name = Column(String, nullable=False)
    workflow_name = Column(String, nullable=True)  # optional grouping label
    task_label = Column(String, nullable=False)  # e.g. "convert package X"
    agent_profile = Column(String, nullable=True)  # profile that did the work
    source_terminal_id = Column(String, nullable=True)
    success = Column(Boolean, nullable=False)
    score = Column(Integer, nullable=True)  # optional 0-100 metric
    friction_notes = Column(Text, nullable=False, default="")  # short, content-free
    created_at = Column(DateTime(timezone=True), default=_utcnow)


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


def _ensure_db_dir() -> None:
    """Create the DB dir owner-only (0o700).

    The DB stores sensitive data (workflow spec_snapshot carries full prompt
    bodies + inputs_json), so the dir is owner-only — the same posture as
    claude_code prompt files (0o600) and the audit log (0o700/0o600). mkdir's
    mode is ignored when the dir already exists (exist_ok) and is masked by
    umask on creation — the chmod enforces 0o700 in both cases, best-effort.
    """
    DB_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(DB_DIR, 0o700)
    except OSError as e:
        logger.warning(f"Could not restrict DB dir permissions on {DB_DIR}: {e}")


# Module-level singletons
_ensure_db_dir()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Initialize database tables and apply schema migrations."""
    _migrate_project_aliases_schema()
    Base.metadata.create_all(bind=engine)
    _restrict_db_file_permissions()
    _migrate_terminals_schema()
    _migrate_memory_indexes()
    _migrate_add_access_count()
    _migrate_add_last_compiled_at()
    _migrate_add_related_keys()
    _migrate_workflow_index()
    _migrate_workflow_run()
    _migrate_workflow_run_indexes()
    _migrate_workflow_run_step()
    _migrate_workflow_outcome_indexes()
    _migrate_workflow_run_event()
    _migrate_workflow_run_seq()
    # Appended LAST (issue #511). Disjoint from the workflow_run* tables that
    # #504 also migrates, so registry order is immaterial — never reorder the
    # entries above.
    _migrate_memory_relationships()
    # Appended LAST (issue #583 Bolt 2, ``approval-store``). Disjoint from every table above —
    # its own new table, no shared columns — so registry order is immaterial here too.
    _migrate_workflow_plan_approval()


def _restrict_db_file_permissions() -> None:
    """Chmod the SQLite file (+ -wal/-shm siblings if present) to 0o600.

    The DB persists sensitive data (workflow spec_snapshot prompt bodies,
    inputs_json), matching the owner-only posture of prompt files and the audit
    log. Called after ``create_all`` so the file exists. Best-effort: a chmod
    failure (exotic filesystems) degrades permissions only, never blocks startup.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE

    for path in (
        DATABASE_FILE,
        DATABASE_FILE.with_name(DATABASE_FILE.name + "-wal"),
        DATABASE_FILE.with_name(DATABASE_FILE.name + "-shm"),
    ):
        if not path.exists():
            continue
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.warning(f"Could not restrict DB file permissions on {path}: {e}")


def _migrate_project_aliases_schema() -> None:
    """Rebuild project_aliases if it predates the alias-only primary key.

    The table originally used a composite PK ``(project_id, alias)``, which
    allowed one alias to map to several project_ids and made reverse lookups
    nondeterministic. The new schema keys on ``alias`` alone. SQLite cannot
    alter a primary key in place, so drop and recreate. The table is an
    opportunistic identity cache rebuilt by ``resolve_project_id`` on demand,
    so dropping rows is safe. Runs before ``create_all`` so the fresh schema
    is created with the new PK.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master " "WHERE type='table' AND name='project_aliases'"
            ).fetchone()
            if row is None:
                return  # table doesn't exist yet — create_all builds it fresh
            cols = conn.execute("PRAGMA table_info(project_aliases)").fetchall()
            # PRAGMA returns rows: (cid, name, type, notnull, dflt_value, pk).
            # In the legacy schema both project_id and alias have pk>0; in the
            # new schema only alias does.
            pk_cols = {c[1] for c in cols if c[5]}
            if pk_cols != {"alias"}:
                conn.execute("DROP TABLE project_aliases")
                conn.commit()
                logger.info("Migration: rebuilt project_aliases with alias-only primary key")
    except Exception as e:
        logger.debug(f"project_aliases migration skipped: {e}")


def _migrate_memory_indexes() -> None:
    """Add explicit indexes on memory_metadata for query performance."""
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_metadata (scope, scope_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_metadata (updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_metadata (memory_type)"
            )
    except Exception as e:
        logger.debug(f"Memory index migration skipped: {e}")


def _migrate_add_access_count() -> None:
    """Add access_count and last_accessed_at columns to memory_metadata if missing.

    Idempotent: PRAGMA table_info gate, ALTER TABLE ADD COLUMN only
    when missing. Fresh DBs already have the columns from
    ``Base.metadata.create_all``. Existing rows get ``0`` / ``NULL`` — the
    correct values for "never recalled".
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            cursor = conn.execute("PRAGMA table_info(memory_metadata)")
            columns = {row[1] for row in cursor.fetchall()}
            if "access_count" not in columns:
                conn.execute(
                    "ALTER TABLE memory_metadata ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("Migration: added access_count column to memory_metadata")
            if "last_accessed_at" not in columns:
                conn.execute("ALTER TABLE memory_metadata ADD COLUMN last_accessed_at DATETIME")
                logger.info("Migration: added last_accessed_at column to memory_metadata")
    except Exception as e:
        logger.debug(f"Migration check for access_count failed: {e}")


def _migrate_add_last_compiled_at() -> None:
    """Add last_compiled_at column to memory_metadata if missing.

    Idempotent: skipped on fresh DBs (the column ships in the model) and on
    repeated runs. Existing Phase 1/2 rows get NULL — correct, since they were
    never LLM-compiled.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            cursor = conn.execute("PRAGMA table_info(memory_metadata)")
            columns = {row[1] for row in cursor.fetchall()}
            if "last_compiled_at" not in columns:
                conn.execute("ALTER TABLE memory_metadata ADD COLUMN last_compiled_at DATETIME")
                logger.info("Migration: added last_compiled_at column to memory_metadata")
    except Exception as e:
        logger.debug(f"Migration check for last_compiled_at failed: {e}")


def _migrate_add_related_keys() -> None:
    """Add related_keys column to memory_metadata if missing.

    Reuses the idempotent ALTER pattern: PRAGMA table_info gate, ALTER TABLE
    ADD COLUMN only when missing. The CHECK(length < 1024) constraint applies
    to FRESH DBs only — adding a CHECK to an existing SQLite table requires a
    full table rebuild we deliberately avoid. Existing DBs rely on the
    parse-side 1024-byte cap in ``_parse_related_keys``.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            cursor = conn.execute("PRAGMA table_info(memory_metadata)")
            columns = {row[1] for row in cursor.fetchall()}
            if "related_keys" not in columns:
                conn.execute("ALTER TABLE memory_metadata ADD COLUMN related_keys TEXT")
                logger.info("Migration: added related_keys column to memory_metadata")
    except Exception as e:
        logger.debug(f"Migration check for related_keys failed: {e}")


def _migrate_memory_relationships() -> None:
    """Create the ``memory_relationships`` table + indexes and backfill legacy
    links (issue #511). Appended LAST to the ``init_db()`` registry.

    Idempotent, zero-arg, self-connecting — mirrors the existing migrators.
    Failure is logged at debug and never propagated (a missing table is
    recoverable; the service degrades). ``CREATE TABLE IF NOT EXISTS`` covers
    existing DBs where ``Base.metadata.create_all`` (which builds the model with
    its CHECK constraints on fresh DBs) has already run or will run — the same
    fresh-vs-existing split the codebase uses for ``related_keys``.

    Disjoint from the ``workflow_run*`` tables (#504); registry order is
    immaterial.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_relationships ("
                "id TEXT PRIMARY KEY, "
                "scope TEXT NOT NULL, "
                "scope_id TEXT NOT NULL, "
                "source_key TEXT NOT NULL, "
                "target_key TEXT NOT NULL, "
                "type TEXT NOT NULL, "
                "origin TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'active', "
                "confidence REAL, "
                "rank INTEGER, "
                "attributes_json TEXT, "
                "source_updated_at DATETIME, "
                "created_at DATETIME, "
                "updated_at DATETIME"
                ")"
            )
            # Dedup UNIQUE index — total because scope_id is NOT NULL (sentinel),
            # so ON CONFLICT fires for all scopes including global.
            #
            # ACCEPTED REDUNDANCY on a FRESH db (human review, PR #524): there,
            # create_all() has already satisfied the model's UniqueConstraint via
            # an unnamed sqlite_autoindex, so this statement adds a SECOND index
            # over identical columns (the name matches the constraint, but SQLite
            # does not treat a table-level UNIQUE as a named index, so
            # IF NOT EXISTS does not suppress it). Kept deliberately: this
            # migrator must remain zero-arg and idempotent for EXISTING dbs,
            # where CREATE TABLE IF NOT EXISTS is a no-op and this is the ONLY
            # thing that establishes the dedup index that replace_set/create rely
            # on. Making it fresh-db-aware would mean probing pragma index_list
            # and branching — more moving parts in a path whose failure mode is
            # silent duplicate edges. The cost is one extra index on new
            # installs: some write amplification and disk, no correctness or
            # query-plan impact.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_rel ON memory_relationships "
                "(scope, scope_id, source_key, target_key, type, origin)"
            )
            # Lookup index for the common (scope, scope_id, source_key) read path.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_rel_lookup ON memory_relationships "
                "(scope, scope_id, source_key)"
            )
            conn.commit()
            _backfill_legacy_related_keys(conn)
    except Exception as e:
        logger.debug(f"memory_relationships migration skipped: {e}")


def _migrate_workflow_plan_approval() -> None:
    """Create the durable ``workflow_plan_approval`` table if missing (issue #583 Bolt 2, ``approval-store``).

    FR-8's re-approval mechanism: one row per APPROVED PLAN, keyed by the ``plan_id`` that
    ``plan_identifier.compute`` derives from a run's execution-affecting fields. A changed plan produces a
    different ``plan_id``, finds no row, and is refused until it is approved in its own right.

    ``plan_id`` IS THE PRIMARY KEY, so one-approval-per-plan is enforced by the database rather than by code
    remembering to check. Combined with ``INSERT OR IGNORE`` in ``services/approval_store.py``, that makes an
    approval WRITE-ONCE: a repeated grant cannot overwrite the original ``approved_at`` / ``approved_by``, and
    there is no update path at all. That absence is deliberate and is the unit's central control — an update
    would let an existing approval be pointed at a changed plan, so the row would read as approved while the
    work behind it had never been reviewed.

    KEYED BY PLAN, NOT BY RUN, and deliberately carrying NO foreign key to ``workflow_run``: an approval's
    lifetime is independent of any run, so deleting a run must not be able to revoke one.

    Idempotent, zero-arg, self-connecting; failure logged at debug and never propagated (B4-BR-1 / B4-RD-4),
    same precedent as every migrator above. Because that failure is SILENT, the table's existence is VERIFIED
    rather than assumed: see ``test/services/test_approval_store.py`` for the ``PRAGMA table_info`` assertion on
    a fresh database. A missing table makes every approval lookup answer False, which refuses every run —
    fail-closed, but diagnosed far from its cause.

    NOT registered in ``workflow_journal``'s ``_REQUIRED_RUN_COLUMNS`` / ``_REQUIRED_STEP_COLUMNS``. That
    verification is scoped to the columns the JOURNAL's own SQL reads, and this table is read by neither, so
    coupling the journal's connection cache to it would be wrong. The consequence is that this table gets no
    runtime self-healing the way ``manifest_json`` does; ``approval_store._connect`` runs this migrator on every
    connect instead, so a transient failure is retried on the next operation.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_plan_approval ("
                "plan_id TEXT PRIMARY KEY, "
                "approved_at TEXT NOT NULL, "
                "approved_by TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_plan_approval migration skipped: {e}")


def _backfill_legacy_related_keys(conn: Any) -> None:
    """One-time, idempotent backfill of ``memory_metadata.related_keys`` into
    ``memory_relationships`` as ``type=relates_to, origin=legacy_related_keys,
    status=active, confidence=NULL`` rows (issue #511, FR-1.4/FR-1.5).

    - Gated per source memory: if any ``legacy_related_keys`` row already exists
      for that ``(scope, scope_id, source_key)``, the source is skipped, so
      re-running ``init_db()`` is a no-op (idempotent).
    - ``related_keys IS NULL`` or ``""`` yields zero rows (never-computed /
      computed-empty carry no edge). The NULL-vs-"" marker stays on
      ``related_keys`` UNCHANGED — this backfill only READS it (ADR-4).
    - ``confidence`` is always NULL (never fabricated — NFR-2.1). Order is
      preserved as ``rank``.
    - A target that no longer resolves to an in-scope memory, a self-link, or a
      key that fails the sanitiser is REPORTED (logged) and NOT written active
      (FR-1.5) — never silently activated.
    - ``scope_id`` is normalised to the sentinel ``""`` for global/federated so
      the dedup index is total.

    Best-effort: any failure is logged at debug and never propagated (the
    service can compute relationships later; a partial backfill is safe because
    the per-source gate resumes cleanly).
    """
    # Lazy import to avoid a circular import (memory_service imports database).
    try:
        from cli_agent_orchestrator.services.memory_service import MemoryService
    except Exception as e:  # pragma: no cover - import guard
        logger.debug(f"backfill skipped (memory_service import): {e}")
        return

    now_iso = _utcnow().isoformat()
    reported: list[str] = []
    try:
        rows = conn.execute(
            "SELECT key, scope, scope_id, related_keys, updated_at "
            "FROM memory_metadata "
            "WHERE related_keys IS NOT NULL AND related_keys != ''"
        ).fetchall()
    except Exception as e:
        logger.debug(f"backfill skipped (memory_metadata read): {e}")
        return

    for key, scope, scope_id, related_keys, src_updated_at in rows:
        sentinel = scope_id if scope_id is not None else RELATIONSHIP_SCOPE_ID_SENTINEL
        # Per-source idempotency gate (exact = on the sentinel, never IS NULL).
        existing = conn.execute(
            "SELECT 1 FROM memory_relationships "
            "WHERE source_key = ? AND scope = ? AND scope_id = ? "
            "AND origin = 'legacy_related_keys' LIMIT 1",
            (key, scope, sentinel),
        ).fetchone()
        if existing is not None:
            continue

        targets = MemoryService._parse_related_keys(related_keys, scope)
        # Resolve which target keys actually exist in the SAME (scope, scope_id).
        for rank, target in enumerate(targets):
            if target == key:
                reported.append(f"{scope}/{scope_id}/{key}->{target}: self-link")
                continue
            # Endpoint existence against memory_metadata: scope_id is genuinely
            # nullable there (real NULL for global), so match logical NULL, NOT
            # the sentinel.
            if scope_id is None:
                found = conn.execute(
                    "SELECT 1 FROM memory_metadata "
                    "WHERE key = ? AND scope = ? AND scope_id IS NULL LIMIT 1",
                    (target, scope),
                ).fetchone()
            else:
                found = conn.execute(
                    "SELECT 1 FROM memory_metadata "
                    "WHERE key = ? AND scope = ? AND scope_id = ? LIMIT 1",
                    (target, scope, scope_id),
                ).fetchone()
            if found is None:
                reported.append(f"{scope}/{scope_id}/{key}->{target}: dangling")
                continue
            try:
                conn.execute(
                    "INSERT INTO memory_relationships "
                    "(id, scope, scope_id, source_key, target_key, type, origin, "
                    "status, confidence, rank, attributes_json, source_updated_at, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'relates_to', 'legacy_related_keys', "
                    "'active', NULL, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT (scope, scope_id, source_key, target_key, type, origin) "
                    "DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        scope,
                        sentinel,
                        key,
                        target,
                        rank,
                        src_updated_at,
                        now_iso,
                        now_iso,
                    ),
                )
            except Exception as e:
                logger.debug(f"backfill insert skipped for {key}->{target}: {e}")
    try:
        conn.commit()
    except Exception:  # pragma: no cover
        pass
    if reported:
        logger.warning(
            "memory_relationships backfill reported %d stale/malformed legacy "
            "links (NOT activated): %s",
            len(reported),
            "; ".join(reported[:20]),
        )


def _migrate_workflow_index() -> None:
    """Create/upgrade the derived ``workflow_index`` table (issue #312, N2).

    The table is a **derived, non-authoritative** projection of the workflow
    spec YAML files on disk (B2-BR-2): it can be dropped and rebuilt
    byte-identically from the files alone (``rebuild_index_from_files``). It
    carries no run/execution state — runs and per-step state are N5/N6.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors the existing ``_migrate_memory_indexes`` pattern. Failure is logged
    at debug and never propagated (a missing index table is recoverable: the
    next ``list`` rebuilds it).

    U5 additively widens ``step_count`` to nullable: script-tier rows carry
    NULL (step count is run-time-determined, unknowable at index time), while
    YAML rows keep populating an int. ``CREATE TABLE IF NOT EXISTS`` only
    covers fresh DBs — on a pre-U5 DB the column already exists as NOT NULL,
    and SQLite cannot ``ALTER COLUMN`` to relax a NOT NULL constraint in
    place. Same drop/rebuild precedent as ``_migrate_project_aliases_schema``:
    the table is fully derived, so dropping it is safe — the next ``list``
    rebuilds it from the workflow files on disk.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_index'"
            ).fetchone()
            if row is not None:
                cols = conn.execute("PRAGMA table_info(workflow_index)").fetchall()
                # PRAGMA row: (cid, name, type, notnull, dflt_value, pk).
                step_count_col = next((c for c in cols if c[1] == "step_count"), None)
                if step_count_col is not None and step_count_col[3]:  # notnull flag set
                    conn.execute("DROP TABLE workflow_index")
                    conn.commit()
                    logger.info(
                        "Migration: rebuilt workflow_index with nullable step_count "
                        "(dropped legacy table; rebuilt from workflow files on next list)"
                    )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_index ("
                "name TEXT PRIMARY KEY, "
                "source_path TEXT NOT NULL, "
                "mode TEXT NOT NULL, "
                "step_count INTEGER, "  # nullable: script-tier rows carry NULL
                "description TEXT NOT NULL DEFAULT '', "
                "indexed_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived table; rebuilt on next list
        logger.debug(f"workflow_index migration skipped: {e}")


def _migrate_workflow_run() -> None:
    """Create the durable ``workflow_run`` journal table if missing (issue #312, N6).

    The run aggregate root: one row per run, keyed by ``run_id`` (E1,
    domain-entities). Per Q1=B this is the **source of truth** for run execution
    state; the Bolt-3 in-memory ``run_registry`` is a cache over it. No loop
    columns (``iteration_counter`` etc.) — deferred to N8 (Q4=B, B4-BR-12).

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors ``_migrate_workflow_index`` (B2, B4-BR-1). Failure is logged at debug
    and never propagated: a missing table is recoverable, the next write retries
    the path and the live run completes on the in-memory floor (B4-RD-4).

    U3 (issue #312, script-tier journal extension) additively appends two
    columns — ``tier`` and ``generation`` (E1, domain-entities) — via the same
    idempotent ``PRAGMA table_info`` gate used by ``_migrate_add_access_count`` /
    ``_migrate_add_related_keys``. Both default to values that make a pre-U3 /
    YAML row read identically to its pre-extension form (INV-1/INV-2): existing
    rows back-fill to ``tier='yaml'``, ``generation='1'``. ``generation`` is TEXT,
    not INTEGER, so it compares byte-identically against the env-var-transported
    string generation value (domain-entities B4 fix).

    ``manifest-column`` (issue #583 Bolt 2, ADR-583-12) additively appends ONE
    column, ``manifest_json``, through the same PRAGMA-gated idiom — the frozen
    execution manifest envelope, carrying source hash, inputs, repository and
    worktree baseline, provider, model, profile, permissions, limits, retry
    policy, the resolved-memory record, and the ``plan_id`` derived from them.
    ``DEFAULT NULL`` means "manifest absent", which every pre-Bolt-2 row is, so
    such a row reads back observably identical to its pre-extension form
    (INV-1/INV-2) and no back-fill is attempted — a manifest records how a run was
    LAUNCHED, which is not recoverable for a run that already started.

    Because this body's failure is silent (see the ``except`` below), the column's
    existence is VERIFIED rather than assumed: ``test_workflow_run_columns`` in
    ``test/clients/test_workflow_run_migration.py`` asserts it on a fresh database,
    with its TEXT type and NULL default. A silent failure would otherwise surface
    far from its cause, as every run losing its manifest — which the Bolt 2
    approval gate reads as "never approved" and refuses. That direction is
    fail-closed, but the diagnosis is still remote, hence the assertion.

    This column is NOT indexed (ADR-583-12: re-approval compares a ``plan_id``
    read out of the envelope and no query filters on it). Writing and reading it
    belong to the ``manifest-freeze`` unit, not to this migrator.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run ("
                "run_id TEXT PRIMARY KEY, "
                "workflow_name TEXT NOT NULL, "
                "spec_snapshot TEXT NOT NULL, "
                "inputs_json TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "current_step_id TEXT, "
                "started_at TEXT NOT NULL, "
                "finished_at TEXT"
                ")"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_run)")}
            if "tier" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run ADD COLUMN tier TEXT NOT NULL DEFAULT 'yaml'"
                )
                logger.info("Migration: added tier column to workflow_run")
            if "generation" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run ADD COLUMN generation TEXT NOT NULL DEFAULT '1'"
                )
                logger.info("Migration: added generation column to workflow_run")
            if "manifest_json" not in columns:
                conn.execute("ALTER TABLE workflow_run ADD COLUMN manifest_json TEXT DEFAULT NULL")
                logger.info("Migration: added manifest_json column to workflow_run")
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_run migration skipped: {e}")


def _migrate_workflow_run_step() -> None:
    """Create the durable ``workflow_run_step`` table if missing (issue #312, N6).

    Per-step durable state: one row per ``(run_id, step_id)`` (E2,
    domain-entities). ``reprompted``/``terminal_id`` are deliberately NOT
    journaled (F3) — they are in-memory-only and defaulted on rebuild. No
    ``which_guard_fired``/``iterations_run`` columns — N8 adds them via its own
    additive migrator (Q4=B, B4-BR-12).

    Idempotent, zero-arg, self-connecting; failure logged at debug and never
    propagated (B4-BR-1 / B4-RD-4), same precedent as ``_migrate_workflow_index``.

    U3 (issue #312, script-tier journal extension) additively appends
    ``call_fingerprint`` (E2, domain-entities) via the same idempotent
    ``PRAGMA table_info`` gate. Defaults to ``NULL`` so a pre-U3 / YAML row is
    indistinguishable from its pre-extension form (INV-1/INV-2); ``append_step``
    is the sole write path for the column (``update_step`` stays untouched — the
    fingerprint is set once, at the RUNNING insert).

    U1 (issue #504, event-log substrate) additively appends three nullable
    columns via the same PRAGMA-gated ``ALTER TABLE ADD COLUMN`` idiom:
    ``terminal_id`` (associated terminal), ``reprompted`` (reprompt flag), and
    ``error_kind`` (structured error kind). All default to ``NULL`` so a
    pre-U1 row reads back observably identical to its pre-extension form
    (additive-only, C-1/C-4). ``workflow_run`` itself is untouched.

    ``result-envelope`` (issue #583, BR-7) then additively appends ONE column,
    ``result_json``, through the same gate — the serialised ``StepResultEnvelope``
    that replay returns (FR-1). ``DEFAULT NULL`` means every pre-#583 row reads as
    "envelope absent" (BR-10), which is safe rather than a gap: such a row's
    fingerprint is legacy-scheme or NULL, so FR-6 already keeps it off the replay
    path and the two guards agree instead of disagreeing.

    Because the failure is silent, the column's existence is VERIFIED rather than
    assumed (BR-8): see ``test/services/test_step_result.py`` for the
    ``PRAGMA table_info`` assertion on a fresh database. A silent failure would
    otherwise surface far away as every settle losing its envelope, which the
    replay gate would read as crash-window rows and halt on.

    MERGE NOTE (2026-08-17, #583 x #504). #583's original rationale for adding ONE
    column rather than three read: "three statements would triple the chance of a
    partial, silent migration", because this body is wrapped in
    ``except Exception`` -> ``logger.debug``. **That argument is overtaken by the
    merge** — #504 independently added three columns to the same silently-failing
    block, so the combined body now issues FOUR guarded ``ALTER`` statements, not
    one. The risk #583 minimised is materially larger than either change assumed
    alone. #583's mitigation (assert the column exists on a fresh database) is
    therefore MORE load-bearing after this merge. Flagged rather than silently
    reconciled.

    CORRECTION (2026-08-18, issue #583 Bolt 2, unit ``manifest-column``). The
    sentence above previously ended by claiming that "#504's three columns have no
    equivalent assertion". **That claim was false and has been removed.** All three
    ARE asserted, in ``test/clients/test_workflow_run_migration.py``: ``terminal_id``,
    ``reprompted`` and ``error_kind`` appear in ``test_workflow_run_step_columns``'s
    exact ``set(cols) == {...}`` column set, and each carries a nullable check
    (``[3] == 0``) plus a default check (``[4] == "NULL"``) in the same test. The
    locations are named here so the denial cannot rot back: a reader who believed it
    would add a duplicate assertion to close a gap that does not exist. What DOES
    survive from the note above is the crowding itself — four guarded ``ALTER``
    statements under one silent ``except`` is a real and growing risk, and adopting a
    migration framework for it is recorded as a candidate decision (out of scope for
    a single additive column).
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run_step ("
                "run_id TEXT NOT NULL, "
                "step_id TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "attempts INTEGER NOT NULL, "
                "output_json TEXT, "
                "error TEXT, "
                "updated_at TEXT NOT NULL, "
                "PRIMARY KEY (run_id, step_id)"
                ")"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_run_step)")}
            if "call_fingerprint" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN call_fingerprint TEXT DEFAULT NULL"
                )
                logger.info("Migration: added call_fingerprint column to workflow_run_step")
            if "terminal_id" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN terminal_id TEXT DEFAULT NULL"
                )
                logger.info("Migration: added terminal_id column to workflow_run_step")
            if "reprompted" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN reprompted INTEGER DEFAULT NULL"
                )
                logger.info("Migration: added reprompted column to workflow_run_step")
            if "error_kind" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN error_kind TEXT DEFAULT NULL"
                )
                logger.info("Migration: added error_kind column to workflow_run_step")
            if "result_json" not in columns:
                conn.execute(
                    "ALTER TABLE workflow_run_step ADD COLUMN result_json TEXT DEFAULT NULL"
                )
                logger.info("Migration: added result_json column to workflow_run_step")
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_run_step migration skipped: {e}")


def _migrate_workflow_outcome_indexes() -> None:
    """Add indexes on workflow_outcomes for retrospector queries.

    The table itself is created by ``Base.metadata.create_all`` (it ships in
    the model, so fresh and existing DBs both get it). Retrospection filters
    by session and by agent profile over a recency window — index both.
    Idempotent, self-connecting, failure logged at debug — mirrors
    ``_migrate_memory_indexes``.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome_session "
                "ON workflow_outcomes (session_name, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome_agent "
                "ON workflow_outcomes (agent_profile, created_at)"
            )
    except Exception as e:
        logger.debug(f"workflow_outcomes index migration skipped: {e}")


def _migrate_workflow_run_event() -> None:
    """Create the durable append-only ``workflow_run_event`` table if missing (issue #504, U1).

    The event log root: one row per emitted workflow domain event, keyed by the
    composite ``(run_id, seq)`` PRIMARY KEY (ADR-1, domain-entities). Per
    NFR-DUR-1 this table is the authoritative, append-only, versioned record of
    workflow execution — rows are inserted and never updated or reordered; ``seq``
    (a per-run monotonically increasing sequence) is the SOLE ordering authority,
    ``ts`` is display/duration only (BR-5). ``run_id``, ``seq``, ``event_type``,
    ``event_schema_version`` (FR-1.1) and ``ts`` are NOT NULL; the remaining
    columns are nullable and populated where applicable. ``iteration`` and
    ``which_guard_fired`` are RESERVED for a later deterministic-loops feature
    (FR-1.5) and stay NULL in the MVP.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    mirrors ``_migrate_workflow_run`` (C-1/C-4, additive-only). Failure is logged
    at debug and never propagated: a missing table is recoverable, the next
    best-effort append retries the path.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run_event ("
                "run_id TEXT NOT NULL, "
                "seq INTEGER NOT NULL, "
                "event_type TEXT NOT NULL, "
                "event_schema_version INTEGER NOT NULL, "
                "ts TEXT NOT NULL, "
                "step_id TEXT, "
                "attempt INTEGER, "
                "state TEXT, "
                "elapsed_ms INTEGER, "
                "provider TEXT, "
                "agent_profile TEXT, "
                "engine TEXT, "
                "terminal_id TEXT, "
                "terminal_offset_start INTEGER, "
                "terminal_offset_len INTEGER, "
                "error_kind TEXT, "
                "reason TEXT, "
                "validation_result TEXT, "
                "output_ref TEXT, "
                "iteration INTEGER, "
                "which_guard_fired TEXT, "
                "PRIMARY KEY (run_id, seq)"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug
        logger.debug(f"workflow_run_event migration skipped: {e}")


def _migrate_workflow_run_seq() -> None:
    """Create the durable ``workflow_run_seq`` high-water table if missing (issue #504, U1).

    One row per run: ``high_water`` records the highest per-run ``seq`` ever
    ALLOCATED (best-effort persisted before the matching event append), so a
    rebuild can resume strictly above any allocated slot even when its append was
    swallowed (BR-3). ``high_water`` advances monotonically (BR-11) and is NOT
    NULL; ``run_id`` is the PRIMARY KEY.

    Idempotent (``CREATE TABLE IF NOT EXISTS``), zero-arg and self-connecting —
    same additive-only posture as ``_migrate_workflow_run_event``. Failure is
    logged at debug and never propagated.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_run_seq ("
                "run_id TEXT PRIMARY KEY, "
                "high_water INTEGER NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug
        logger.debug(f"workflow_run_seq migration skipped: {e}")


def _migrate_workflow_run_indexes() -> None:
    """Add explicit indexes on ``workflow_run`` for list-query performance (U1, FR-3.2).

    Two single-column indexes serving the two shapes ``list_runs`` produces: the
    unfiltered newest-first list orders by ``started_at`` alone (served by
    ``idx_workflow_run_started_at``), and the state-filtered list narrows on
    ``state`` (served by ``idx_workflow_run_state``). Two single-column indexes
    cover both paths; a single composite ``(state, started_at)`` would not serve
    the unfiltered ``started_at``-only ordering (ADR-6, IR-1).

    Zero-arg, self-connecting, and idempotent — mirrors ``_migrate_memory_indexes``.
    Each statement uses ``CREATE INDEX IF NOT EXISTS`` so a second ``init_db()`` is
    a no-op (IR-2); no destructive migration, no Alembic (NFR-5). It creates only
    indexes, never columns — so the C-4 exact-column migration test is untouched
    (IR-4). Registered AFTER ``_migrate_workflow_run`` in ``init_db`` so the base
    table exists first. Failure is logged at debug and never raised: a missing
    index degrades to a table scan, not a crash (IR-3).
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_run_started_at "
                "ON workflow_run (started_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_run_state ON workflow_run (state)"
            )
    except Exception as e:  # noqa: BLE001 — missing index degrades to a scan (IR-3)
        logger.debug(f"workflow_run index migration skipped: {e}")


def _migrate_terminals_schema() -> None:
    """Add terminal metadata columns to existing SQLite databases."""
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
        if "shell_command" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN shell_command TEXT")
            conn.commit()
            logger.info("Migration: added shell_command column to terminals table")
        if "caller_id" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN caller_id TEXT")
            conn.commit()
            logger.info("Migration: added caller_id column to terminals table")
        if "engine" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN engine TEXT")
            conn.commit()
            logger.info("Migration: added engine column to terminals table")
        if "group" not in columns:
            # "group" is a SQL reserved word in some dialects but not SQLite;
            # quoted defensively so this ALTER survives if that ever changes.
            conn.execute('ALTER TABLE terminals ADD COLUMN "group" TEXT')
            conn.commit()
            logger.info("Migration: added group column to terminals table")
        if "metadata" not in columns:
            conn.execute('ALTER TABLE terminals ADD COLUMN "metadata" TEXT')
            conn.commit()
            logger.info("Migration: added metadata column to terminals table")
        if "working_directory" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN working_directory TEXT")
            conn.commit()
            logger.info("Migration: added working_directory column to terminals table")
        conn.close()
    except Exception as e:
        logger.warning(f"Migration check for terminals schema failed: {e}")


def create_terminal(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    shell_command: Optional[str] = None,
    caller_id: Optional[str] = None,
    engine: Optional[str] = None,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    working_directory: Optional[str] = None,
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
            working_directory=working_directory,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
            shell_command=shell_command,
            caller_id=caller_id,
            engine=engine,
            group=_json.dumps(group) if group else None,
            metadata_json=_json.dumps(metadata) if metadata else None,
        )
        db.add(terminal)
        db.commit()
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "working_directory": terminal.working_directory,
            "allowed_tools": allowed_tools,
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
            "engine": terminal.engine,
            # Normalized the same way as what was actually stored (an empty
            # container is stored as NULL, same as omitted) -- self-ROAST
            # finding: echoing the raw `group`/`metadata` input here made
            # create_terminal(group=[]) return {"group": []} while an
            # immediately-following get_terminal_metadata() on the same row
            # returns {"group": None}, an API-consistency gap.
            "group": group if group else None,
            "metadata": metadata if metadata else None,
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
        group = _json.loads(terminal.group) if terminal.group else None
        metadata = _json.loads(terminal.metadata_json) if terminal.metadata_json else None
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "working_directory": terminal.working_directory,
            "allowed_tools": allowed_tools,
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
            "engine": terminal.engine or ("v2" if terminal.provider == "kiro_cli" else None),
            "group": group,
            "metadata": metadata,
            "last_active": terminal.last_active,
        }


def update_terminal_group(terminal_id: str, group: Optional[List[str]]) -> bool:
    """Replace a terminal's group array. ``None``/``[]`` clears it (opts out of discovery)."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return False
        terminal.group = _json.dumps(group) if group else None
        db.commit()
        return True


def update_terminal_metadata(terminal_id: str, metadata: Optional[Dict[str, Any]]) -> bool:
    """Replace a terminal's free-form metadata dict. ``None``/``{}`` clears it."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return False
        terminal.metadata_json = _json.dumps(metadata) if metadata else None
        db.commit()
        return True


def get_terminal_group(terminal_id: str) -> Optional[List[str]]:
    """Return a terminal's own group array, or None if unset or the terminal doesn't exist."""
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal or not terminal.group:
            return None
        return cast(List[str], _json.loads(terminal.group))


def list_siblings_by_group_prefix(
    caller_id: str,
    prefix: List[str],
    caller_session: Optional[str] = None,
    cross_session: bool = False,
) -> List[Dict[str, Any]]:
    """Return ``{id, group, metadata}`` for every OTHER terminal sharing ``prefix``.

    ``prefix`` is the caller's own group truncated to the (already-clamped)
    depth — this function does no clamping itself, it only matches. A
    candidate terminal with no group, or a group shorter than ``len(prefix)``,
    is excluded rather than compared partially or raising (#432).

    Session-scoped by default (issue #432 design discussion, tedswinyar +
    klabulan, 2026-07-17/18): ``caller_session`` (the caller's own
    ``tmux_session``) is an implicit, non-bypassable first filter ON TOP of
    the group-prefix match, unless ``cross_session=True`` is explicitly
    passed. Without this, two unrelated CAO sessions that happen to reuse
    the same ``group`` prefix (a naming collision, a copy-pasted template,
    two features that picked the same tenant/project id) would silently
    discover each other -- the same class of "implicitly-scoped state that
    turns out not to be" mistake cited in that discussion's incident
    history. Cross-session discovery is a legitimate use case and stays
    available, just opt-in rather than the unstated default.

    ``group`` is stored JSON-encoded (see ``TerminalModel.group``), so the
    query prefilters with a SQL ``LIKE`` prefix match on that encoding before
    loading/decoding candidate rows in Python (Copilot review, PR #433) —
    without it this scanned and JSON-decoded every grouped terminal on the
    server regardless of how narrow ``prefix`` is. Because ``json.dumps``
    closes each string element in a quote immediately, the encoded prefix
    (full array minus its trailing ``]``) can't false-positive match a
    longer sibling element that merely shares a text prefix (e.g. prefix
    element ``"project_5"`` vs. a sibling group containing ``"project_50"``
    — the sibling's extra ``0`` before its closing ``"`` breaks the SQL
    match). The exact Python-level comparison below is kept regardless, as
    the source of truth — the SQL match only narrows candidates (a prefilter
    defect can only cause a false negative here, i.e. a missed perf win,
    never a false positive / correctness or security regression).

    This SQL-level match assumes the stored ``group`` was encoded with the
    same ``json.dumps`` defaults used below (notably ``ensure_ascii=True``,
    the default) — true today of both write paths (``create_terminal`` and
    ``update_terminal_group``), which both use plain ``json.dumps(group)``.
    If either write path ever changes its encoding, this prefilter must
    change with it.

    A single row with corrupt ``group`` JSON (e.g. hand-edited DB, a future
    write-path bug) is logged and excluded rather than raising and failing
    discovery for every OTHER terminal in the same request (tedswinyar, PR
    #433 review). Corrupt ``metadata`` JSON on an otherwise-matching sibling
    is likewise logged and reported back as ``metadata=None`` -- the sibling
    itself is still real and discoverable, only its metadata is unreadable.
    """
    import json as _json

    depth = len(prefix)
    # Encode the prefix array and drop its trailing ']' so this matches both
    # a sibling group of the same length and a longer one that starts with
    # it, e.g. prefix ["a", "b"] -> '["a", "b"' matches '["a", "b"]' and
    # '["a", "b", "c"]'.
    like_prefix = _json.dumps(prefix)[:-1]
    with SessionLocal() as db:
        query = db.query(TerminalModel).filter(
            TerminalModel.id != caller_id,
            TerminalModel.group.isnot(None),
            TerminalModel.group.startswith(like_prefix, autoescape=True),
        )
        if not cross_session and caller_session is not None:
            query = query.filter(TerminalModel.tmux_session == caller_session)
        rows = query.all()
        siblings = []
        for row in rows:
            try:
                sibling_group = _json.loads(row.group)
                if not isinstance(sibling_group, list):
                    raise ValueError(f"decoded to {type(sibling_group).__name__}, expected list")
            except (TypeError, ValueError) as e:
                logger.warning(
                    "list_siblings_by_group_prefix: skipping terminal %s -- "
                    "corrupt group JSON (%s)",
                    row.id,
                    e,
                )
                continue
            if len(sibling_group) < depth:
                continue
            if sibling_group[:depth] == prefix:
                metadata = None
                if row.metadata_json:
                    try:
                        metadata = _json.loads(row.metadata_json)
                    except (TypeError, ValueError) as e:
                        logger.warning(
                            "list_siblings_by_group_prefix: terminal %s has "
                            "corrupt metadata JSON (%s); returning it with "
                            "metadata=None",
                            row.id,
                            e,
                        )
                siblings.append(
                    {
                        "id": row.id,
                        "group": sibling_group,
                        "metadata": metadata,
                    }
                )
        return siblings


def list_terminals_by_session(tmux_session: str) -> List[Dict[str, Any]]:
    """List a tmux session's terminals, oldest first.

    **Index 0 is the session's oldest surviving terminal -- normally its
    conductor.** That is a contract, not an accident of the query plan:
    ``flow_service`` decides whether to kill a session by whether index 0 is
    busy, ``cao session status``/``list`` label index 0 as the Conductor over
    HTTP, and ``session_service`` derives a session's reported profile and
    directory from the earliest terminal that HAS either (#497) -- a first-match
    scan rather than a bare index, so a conductor row with both fields NULL
    still cedes ownership to a worker. Callers should cite this docstring rather
    than restate the rule.

    Ordered by ``rowid``, which is insertion order, and normally creation order:
    every row is written at one site (``db_create_terminal``, called from
    ``terminal_service.create_terminal``), and a worker's row cannot precede its
    conductor's because the MCP handoff either resolves a caller that already
    has a row (``GET /terminals/{caller_id}``, which raises if the id is stale)
    or, when it is running outside a CAO terminal, records no caller at all and
    starts a NEW session in which the worker is itself the conductor. Reuse
    after deletion does not reorder surviving rows -- see ``TerminalModel`` for
    the mechanism and its limits.

    Two known gaps, both pre-existing and neither introduced here:

    * ``session_lifecycle_lock`` serialises creation against teardown, but it is
      a ``threading.Lock`` and therefore per-PROCESS. ``cao schedule run`` calls
      ``execute_flow`` in the CLI process, and ``flow_service`` does not take the
      lock at all, so its kill-and-recreate is not serialised against cao-server.
      A worker insert landing inside that window can take index 0.
    * Index 0 is the oldest SURVIVING row, which is not the conductor once the
      conductor's own row is gone -- ``DELETE /terminals/{id}`` has no guard, the
      MCP tool exposes it, and ghost-terminal cleanup deletes rows while the
      session lives on.

    Both mean consumers should treat index 0 as best-effort, which is what
    ``_enrich_session_ownership`` already does. The ordering is what makes it
    *predictable*; it does not make it an identity.

    This ORDER BY does not change what this function returned before it was
    added -- an unordered scan of a rowid table already yielded rowid order.
    It states the order instead of inheriting it, so an index added later
    cannot quietly change which terminal is the conductor.
    """
    with SessionLocal() as db:
        terminals = (
            db.query(TerminalModel)
            .filter(TerminalModel.tmux_session == tmux_session)
            .order_by(literal_column("terminals.rowid"))
            .all()
        )
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "working_directory": t.working_directory,
                "engine": t.engine or ("v2" if t.provider == "kiro_cli" else None),
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


def update_terminal_shell_command(terminal_id: str, shell_command: str) -> bool:
    """Update the shell_command baseline for a terminal."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.shell_command = shell_command
            db.commit()
            return True
        return False


def list_terminals_in_sessions(tmux_sessions: List[str]) -> List[Dict[str, Any]]:
    """List terminals for several tmux sessions in one query.

    Exists so ``list_sessions`` can enrich N sessions without N queries (issue
    #629) while still reading only the rows it will use. ``list_all_terminals``
    would also collapse the query count, but its cost scales with the whole
    table — including rows for sessions tmux no longer reports, which accumulate
    because ``cleanup_service.cleanup_old_data`` only runs at server startup, so
    a long-uptime server never sweeps them. Bounding the read by the live
    session names keeps the cost proportional to the workload instead of to the
    leak.

    Ordered by ``rowid``, identically to ``list_terminals_by_session`` -- see
    that function for the index-0-is-the-conductor contract and why rowid
    expresses creation order. The two MUST order the same way: the caller picks
    a session's "first known terminal", so the pick is order-sensitive, and an
    ordered batched read beside an unordered per-session read is what let this
    function silently disagree with the one it replaced.

    The ORDER BY is not decoration. Without it the order is whatever the engine
    plans: today every plan is a rowid scan (there is no index on
    ``tmux_session``), but adding one -- the obvious reaction to a slow
    per-session lookup -- reorders the probe. Measured, a plain ASC index on
    ``(tmux_session, id)`` makes an unordered read return a worker ahead of the
    session creator; ordering here is what keeps the conductor first.

    Ordering by ``id`` would NOT do that: ``id`` is ``uuid4().hex[:8]``
    (utils/terminal.generate_terminal_id), so it makes the conductor a
    deterministic *random* terminal, and a session whose creator happens to sort
    above one of its own workers advertises the worker's profile and directory as
    the session's.

    Returns an empty list without querying when given no session names.
    """
    if not tmux_sessions:
        return []
    with SessionLocal() as db:
        terminals = (
            db.query(TerminalModel)
            .filter(TerminalModel.tmux_session.in_(tmux_sessions))
            .order_by(literal_column("terminals.rowid"))
            .all()
        )
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "working_directory": t.working_directory,
                "engine": t.engine or ("v2" if t.provider == "kiro_cli" else None),
                "last_active": t.last_active,
            }
            for t in terminals
        ]


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
                "working_directory": t.working_directory,
                "engine": t.engine or ("v2" if t.provider == "kiro_cli" else None),
                "last_active": t.last_active,
            }
            for t in terminals
        ]


def list_pending_receiver_ids_by_provider(provider: str) -> List[str]:
    """List receiver terminal IDs with pending messages for a specific provider."""
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel.receiver_id)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(
                TerminalModel.provider == provider,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .distinct()
            .all()
        )
        return [row[0] for row in rows]


def list_pending_receiver_ids_older_than(min_age_seconds: int) -> List[str]:
    """List receiver terminal IDs whose messages have been PENDING too long.

    Returns the distinct receivers of any message still PENDING for longer than
    ``min_age_seconds``. Used by the inbox reconciliation sweep to find messages
    the immediate and watchdog delivery paths missed, without competing with
    them for freshly queued ones (issue #131).

    The join on ``terminals`` drops messages whose receiver terminal no longer
    exists, so the sweep does not keep retrying deliveries to deleted agents.

    ``created_at`` is stored local-naive (``InboxModel.created_at`` defaults to
    ``datetime.now``), so the cutoff uses ``datetime.now()`` to match — the same
    convention as the retention query in ``cleanup_service.cleanup_old_data``.
    """
    cutoff = datetime.now() - timedelta(seconds=min_age_seconds)
    with SessionLocal() as db:
        rows = (
            db.query(InboxModel.receiver_id)
            .join(TerminalModel, TerminalModel.id == InboxModel.receiver_id)
            .filter(
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.created_at < cutoff,
            )
            .distinct()
            .all()
        )
        return [row[0] for row in rows]


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


def delete_terminals_by_ids(terminal_ids: List[str]) -> int:
    """Delete specific terminal rows by id. Returns the number deleted.

    Unlike ``delete_terminals_by_session`` (which deletes EVERY row for a
    session name), this deletes only the given ids. Session teardown uses it to
    scope its reconciliation sweep to the incarnation it started tearing down,
    so a concurrent same-name recreate — whose rows carry freshly generated ids
    — is never swept (#498).
    """
    if not terminal_ids:
        return 0
    with SessionLocal() as db:
        deleted = (
            db.query(TerminalModel)
            .filter(TerminalModel.id.in_(terminal_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted


def create_inbox_message(sender_id: str, receiver_id: str, message: str) -> InboxMessage:
    """Create inbox message with status=MessageStatus.PENDING.

    Raises:
        ValueError: If the receiver terminal does not exist.
    """
    with SessionLocal() as db:
        if not db.query(TerminalModel).filter(TerminalModel.id == receiver_id).first():
            raise ValueError(f"Terminal '{receiver_id}' not found")
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


def record_project_alias(project_id: str, alias: str, kind: str) -> None:
    """Idempotently record a project_id ↔ alias mapping (Phase 2.5 U6).

    Used opportunistically by ``resolve_project_id`` to track historical
    cwd-hash and git-remote-url aliases for a canonical project_id. Best-effort
    only — DB errors are swallowed so identity resolution is never blocked.
    """
    if not project_id or not alias or project_id == alias:
        return
    try:
        with SessionLocal() as db:
            # Upsert by alias (the primary key). If the same alias was already
            # mapped — e.g. recorded against an override id, then re-resolved
            # via git remote — repoint it to the current canonical project_id
            # so reverse lookups stay deterministic instead of duplicating.
            existing = db.query(ProjectAliasModel).filter(ProjectAliasModel.alias == alias).first()
            if existing is None:
                db.add(ProjectAliasModel(project_id=project_id, alias=alias, kind=kind))
                db.commit()
            elif existing.project_id != project_id or existing.kind != kind:
                existing.project_id = project_id
                existing.kind = kind
                db.commit()
    except Exception as e:
        logger.debug(f"record_project_alias failed (non-fatal): {e}")


def get_project_id_by_alias(alias: str) -> Optional[str]:
    """Return the canonical ``project_id`` for an alias, or None if unknown."""
    if not alias:
        return None
    try:
        with SessionLocal() as db:
            row = db.query(ProjectAliasModel).filter(ProjectAliasModel.alias == alias).first()
            return cast(Optional[str], row.project_id) if row else None
    except Exception as e:
        logger.debug(f"get_project_id_by_alias failed (non-fatal): {e}")
        return None


def list_aliases_for_project(project_id: str) -> List[Dict[str, Any]]:
    """List all aliases recorded for a canonical ``project_id``."""
    if not project_id:
        return []
    try:
        with SessionLocal() as db:
            rows = (
                db.query(ProjectAliasModel).filter(ProjectAliasModel.project_id == project_id).all()
            )
            return [{"project_id": r.project_id, "alias": r.alias, "kind": r.kind} for r in rows]
    except Exception as e:
        logger.debug(f"list_aliases_for_project failed (non-fatal): {e}")
        return []


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
            prompt_template=None,
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
            prompt_template=None,
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
                prompt_template=None,
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
                prompt_template=None,
            )
            for f in flows
        ]
