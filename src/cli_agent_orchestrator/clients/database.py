"""Minimal database client with only terminal metadata."""

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, cast

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.exc import OperationalError
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
    allowed_tools = Column(String, nullable=True)  # JSON-encoded list of CAO tool names
    shell_command = Column(String, nullable=True)  # shell process name captured before kiro launch
    caller_id = Column(String, nullable=True)  # terminal that created this one (callback target)
    # Durable, non-reusable incarnation for managed-launch destructive
    # operations. Legacy/operator terminals may be NULL; managed terminals
    # always bind the reservation generation here before provider I/O.
    generation = Column(Text, nullable=True, unique=True)
    # Server-owned immutable pane/window identity (cond-0069 attestation):
    # tmux-assigned ids recorded at creation. Window NAMES are mutable (a
    # worker can rename its own window), pane_id/window_id are not — they are
    # the only tmux-side facts an attestation may bind a supervisor to.
    pane_id = Column(Text, nullable=True)
    window_id = Column(Text, nullable=True)
    # The tmux server that owns that pane id (cond-0078 §24.7). A pane id
    # is unique only within one tmux server and several servers routinely
    # run on one host, so pane_id alone names a pane on *whichever* server
    # a later process happens to reach. NULL on every legacy row, and a
    # NULL never satisfies the writer-boundary check: an unbound terminal
    # refuses control input rather than being written somewhere plausible.
    server_socket_path = Column(Text, nullable=True)
    # The remaining two immutable tmux ids, recorded for the same reason as
    # the pane and window ids. A session NAME is mutable and reusable just
    # as a window name is, so "$N" is what a reattach means when it says
    # "this session". ``pane_pid`` is the pane's primary process, which
    # distinguishes the incarnation that was registered from a later one
    # that inherited its ids; it is one component of the tuple and never a
    # sufficient check by itself — a survival test that consulted only the
    # pid, or only the window, is what previously let a write land in an
    # unrelated live composer.
    session_id = Column(Text, nullable=True)
    pane_pid = Column(Integer, nullable=True)
    # The provider-side session this incarnation is resumable as, projected
    # onto the terminal row rather than left only in the native-attachment
    # table. A human view that has to join another table to answer "which
    # provider session is this card?" will eventually be written without the
    # join, and will then show a card that cannot be traced back to a
    # resumable session.
    native_session_id = Column(Text, nullable=True)
    # Durable lifecycle, so a row that no longer names a live pane can say
    # so instead of reporting a provider status forever. Without it a
    # deleted window's row is indistinguishable from a live worker whose
    # provider state has not been detected yet, and every human view keeps
    # showing it as an ordinary terminal in an unknown state.
    #
    #   live               the recorded identity was observed intact
    #   superseded         the identity resolves to a different incarnation;
    #                      superseded_by_* names the row that replaced it
    #   dead               the recorded pane is provably absent
    #   unknown-liveness   the identity could not be observed at all
    #
    # ``unknown-liveness`` is deliberately not a synonym for dead. "We could
    # not look" is not evidence: such a row is not live, is not reaped, is
    # not attachable, and is never a control or task target.
    lifecycle_state = Column(Text, nullable=True)
    lifecycle_reason = Column(Text, nullable=True)
    liveness_checked_at = Column(DateTime, nullable=True)
    # A demotion points at the incarnation that replaced this one rather
    # than editing this row's identity in place. Re-pointing a live row is
    # the forbidden operation: it is exactly the aliasing that turns a
    # stale row into a writable handle on somebody else's pane.
    superseded_by_terminal_id = Column(Text, nullable=True)
    superseded_by_generation = Column(Text, nullable=True)
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


class SessionEnvModel(Base):
    """SQLAlchemy model for persisted per-session forwarded env vars (issue #248).

    One row per tmux session holding the ``cao launch --env KEY=VALUE`` mapping
    as a JSON object, so the forwarded env survives a cao-server restart and
    windows created post-restart pick it up again. The in-memory map in
    ``services/session_env.py`` is only a cache over this table. Columns are
    TEXT (not VARCHAR) so the ``create_all`` schema matches the raw
    ``_migrate_session_env`` DDL byte-for-byte.

    Values are stored PLAINTEXT. The DB file is already 0600 in a 0700 dir,
    and forwarded values are expected to be non-secret path/routing data —
    do not forward secrets through ``--env``.
    """

    __tablename__ = "session_env"

    session_name = Column(Text, primary_key=True)
    env_vars = Column(Text, nullable=False)  # JSON object: {str: str}
    updated_at = Column(Text, nullable=False)  # ISO-8601 UTC timestamp


class ManagedLaunchReservationModel(Base):
    """Durable identity and evidence for two-phase managed task admission.

    A reservation exists before a provider or terminal is started.  The
    conductor-supplied reservation id is the idempotency key; ``terminal_id``
    and ``generation`` are allocated once and never changed.  JSON payloads
    intentionally remain opaque to the generic database layer so the managed
    launch service owns schema validation and state transitions.
    """

    __tablename__ = "managed_launch_reservations"

    reservation_id = Column(Text, primary_key=True)
    terminal_id = Column(String, nullable=False, unique=True)
    generation = Column(Text, nullable=False, unique=True)
    session_name = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    agent_profile = Column(Text, nullable=False)
    caller_id = Column(Text, nullable=False)
    working_directory = Column(Text, nullable=False)
    trusted_project_root = Column(Text, nullable=True)
    state = Column(Text, nullable=False)
    request_json = Column(Text, nullable=False)
    observations_json = Column(Text, nullable=False, default="[]")
    readiness_json = Column(Text, nullable=True)
    admission_json = Column(Text, nullable=True)
    negative_json = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class ManagedLaunchV2ReservationModel(Base):
    """Isolated managed-launch v2 store (distinct protocol vintage).

    v2 rows live in their own table so every v1 query, deletion, and
    cleanup path (which only knows ``managed_launch_reservations``) has
    zero visibility into v2 state by construction.  ``protocol_vintage``
    is first-class and immutable; v1 rows never gain v2 semantics and v2
    rows never silently downgrade.  The launch nonce is stored only as a
    digest.
    """

    __tablename__ = "managed_launch_v2_reservations"

    reservation_id = Column(Text, primary_key=True)
    terminal_id = Column(String, nullable=False, unique=True)
    generation = Column(Text, nullable=False, unique=True)
    protocol_vintage = Column(Text, nullable=False, default="v2")
    session_name = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    agent_profile = Column(Text, nullable=False)
    caller_id = Column(Text, nullable=False)
    working_directory = Column(Text, nullable=False)
    trusted_project_root = Column(Text, nullable=True)
    obligation_generation = Column(Text, nullable=False)
    task_id = Column(Text, nullable=True)
    run_id = Column(Text, nullable=False)
    launch_nonce_digest = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    request_json = Column(Text, nullable=False)
    binding_json = Column(Text, nullable=True)
    # Journaled bind intent (exact canonical creation/binding/route
    # payload bytes + fencing token), committed BEFORE any immutable
    # external publication so a crash on either side of the SQL/filesystem
    # boundary reconciles against the same bytes.
    bind_intent_json = Column(Text, nullable=True)
    admission_json = Column(Text, nullable=True)
    #: The durable cleanup proof, written once by the first caller to clean
    #: this generation. Additive and nullable: an old binary reading this
    #: table ignores the column rather than failing on it, and an existing
    #: row keeps its bytes.
    #:
    #: Its PRESENCE -- never the absence of the terminal row -- is what lets
    #: a response project ``cleaned``. A missing terminal row is not proof a
    #: cleanup happened: it can be absent for having never existed, or for
    #: having been removed by something else, and projecting from that would
    #: report a cleanup nobody performed.
    cleanup_json = Column(Text, nullable=True)
    # The resolved execution mode ('native_tui' | 'acp') and the
    # precedence level that supplied it.  Nullable on purpose: a row
    # written before the execution-mode contract existed carries NULL,
    # and NULL reads back as legacy ACP.  It must never read as native —
    # a native guard that accepted "mode absent" would treat every
    # historical ACP generation as an attachable native session.
    execution_mode = Column(Text, nullable=True)
    execution_mode_source = Column(Text, nullable=True)
    # The immutable, redacted evidence for a generation that reached
    # ``preflight_blocked`` — reason, redacted detail, the exact
    # reservation/terminal/generation identity, and ``task_bytes_submitted``
    # (always false in this state).  Written once, on the first transition
    # into the blocked state, and never rewritten: recovery must read the
    # original cause, not the last one to fail.  NULL for any row that
    # never blocked.
    preflight_failure_json = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class ManagedLaunchV2TerminalModel(Base):
    """Isolated v2 managed-terminal metadata (distinct protocol vintage).

    v2 managed terminal rows live ONLY here — never in the shared
    ``terminals`` table — so every old-binary query, list, watchdog, and
    cleanup path (which only knows ``terminals``) has zero visibility
    into v2 terminal state by construction.  ``protocol_vintage`` is
    pinned to 'v2'.
    """

    __tablename__ = "managed_launch_v2_terminals"

    id = Column(String, primary_key=True)  # "abc123ef"
    tmux_session = Column(String, nullable=False)
    tmux_window = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    agent_profile = Column(String)
    allowed_tools = Column(String, nullable=True)
    caller_id = Column(String, nullable=True)
    generation = Column(Text, nullable=False, unique=True)
    protocol_vintage = Column(Text, nullable=False, default="v2")
    pane_id = Column(Text, nullable=True)
    window_id = Column(Text, nullable=True)
    # The tmux server owning ``pane_id`` (§24.7); see TerminalModel.
    server_socket_path = Column(Text, nullable=True)
    # The same canonical identity and lifecycle the shared table carries,
    # duplicated here rather than shared. The separation is the whole
    # point of this store — old-binary machine paths must keep zero v2
    # visibility — but a managed terminal still has to answer the identity
    # questions a human view asks of every other terminal, or it can only
    # be made visible by guessing. Column names are prefixed because the
    # vintage receipt records bare names and requires them unique across
    # the v2 surface. See TerminalModel for what each one means.
    v2_session_id = Column(Text, nullable=True)
    v2_pane_pid = Column(Integer, nullable=True)
    v2_native_session_id = Column(Text, nullable=True)
    v2_lifecycle_state = Column(Text, nullable=True)
    v2_lifecycle_reason = Column(Text, nullable=True)
    v2_liveness_checked_at = Column(Text, nullable=True)
    v2_superseded_by_terminal_id = Column(Text, nullable=True)
    v2_superseded_by_generation = Column(Text, nullable=True)
    last_active = Column(DateTime, default=datetime.now)


class NativeSessionAttachmentModel(Base):
    """Exclusive, crash-safe ownership of one provider-native session.

    Keyed by ``(provider, native_session_id)`` — the provider's own
    session identity, not any CAO-side name — so exactly one owner can
    be attached to a given provider session at a time regardless of how
    many terminals, generations, or execution modes reference it.

    The owner tuple is ``(terminal_id, generation, execution_mode,
    pane_id, process_identity)``.  ``execution_mode`` is part of the
    owner precisely so an ACP bridge and a native TUI can never both
    hold the same provider session: a second attach in the other mode
    sees a live owner and is refused rather than silently multiplexed.

    Rows are written with the intent BEFORE the provider is launched, so
    a crash at any point leaves a durable claim that recovery must
    adjudicate.  ``ambiguous`` is a frozen terminal-for-automation
    state: it preserves the owner and is never auto-released.
    """

    __tablename__ = "native_session_attachments"

    provider = Column(Text, primary_key=True)
    native_session_id = Column(Text, primary_key=True)
    state = Column(Text, nullable=False)
    owner_terminal_id = Column(Text, nullable=False)
    owner_generation = Column(Text, nullable=False)
    owner_execution_mode = Column(Text, nullable=False)
    owner_pane_id = Column(Text, nullable=True)
    # Canonical JSON of the owning OS process identity (pid + an
    # start-time/lineage marker).  A bare pid is not identity: pids are
    # recycled, and a recycled pid would forge a survivor.
    owner_process_identity_json = Column(Text, nullable=True)
    # Canonical JSON of the journaled acquire intent, written before any
    # provider I/O so a crash between intent and launch is adjudicable.
    intent_json = Column(Text, nullable=False)
    # Canonical JSON of the accepted no-survivor proof that permitted the
    # last release.  Retained as evidence; never cleared by a later claim.
    release_proof_json = Column(Text, nullable=True)
    ambiguity_reason = Column(Text, nullable=True)
    # Monotonic per-row counter; every CAS transition bumps it so a
    # lost-update race is detectable rather than silently last-write-wins.
    epoch = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class KimiNativeControlOperationModel(Base):
    """One human/orchestrator control operation against a native Kimi TUI.

    Deliberately a separate store from the delivery journal and from every
    ACP receipt kind.  The delivery journal records the truth of ordinary
    task submission; a native control operation is a different act against
    a different surface, and writing one into the other would let a pane
    keystroke rewrite delivery truth.

    Keyed by the caller-minted ``operation_id`` so a lost response is
    resolvable by exact id rather than by re-sending.  The row is written
    with its intent BEFORE anything is typed into the pane, so a crash
    between intent and keystroke leaves a durable record that recovery
    adjudicates instead of a silent maybe-sent.

    ``posted_at`` records that bytes reached the transport and nothing
    more.  It is stored separately from the provider observation on
    purpose: a successful pane write is not provider acceptance, and the
    two must never be readable as the same fact.
    """

    __tablename__ = "kimi_native_control_operations"

    operation_id = Column(Text, primary_key=True)
    kind = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    # The full binding this operation is valid for. A mismatch on any
    # component is refused before the pane is touched, so an operation
    # minted for one generation can never land in its successor.
    provider = Column(Text, nullable=False)
    native_session_id = Column(Text, nullable=False)
    terminal_id = Column(Text, nullable=False)
    generation = Column(Text, nullable=False)
    execution_mode = Column(Text, nullable=False)
    # Present only for a steer, which binds to the exact active turn it
    # intends to interrupt. A queue operation has no turn by definition.
    turn_id = Column(Text, nullable=True)
    # The digest of the exact literal payload rather than the payload
    # itself: enough to prove the same operation was not re-sent with
    # different bytes, without durably storing message content here.
    payload_sha256 = Column(Text, nullable=False)
    # Canonical JSON of the intent journaled before any side effect.
    intent_json = Column(Text, nullable=False)
    # Canonical JSON of the transport-level observation: the digest of
    # what was written, and that Enter went as its own explicit key after
    # the literal text. Facts about what this process did -- never a claim
    # about what the provider received.
    transport_json = Column(Text, nullable=True)
    # Canonical JSON of the provider-side observation that justified
    # acceptance, completion, or refusal. Absent means no provider fact
    # was ever observed -- which is exactly why such a row cannot read as
    # accepted.
    observation_json = Column(Text, nullable=True)
    posted_at = Column(Text, nullable=True)
    refusal_reason = Column(Text, nullable=True)
    ambiguity_reason = Column(Text, nullable=True)
    # Monotonic per-row counter; every CAS transition bumps it so two
    # concurrent operators racing the same operation are detected rather
    # than silently last-write-wins.
    epoch = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class ClaudeNativeControlOperationModel(Base):
    """One control operation against a native Claude TUI.

    Structurally the twin of the Kimi control store and deliberately a
    *separate table*, not a shared one with a provider column. The
    separation is what makes a Kimi operation unable to satisfy a Claude
    check, and vice versa: the two providers' composer facts, refusal
    reasons and acceptance evidence are different, and one table would
    make cross-provider confusion a query away rather than impossible.

    Every other property is the same, and for the same reasons. Keyed by
    the caller-minted ``operation_id`` so a lost response is resolvable by
    exact id rather than by re-sending. The intent is written before
    anything is typed, so a crash between intent and keystroke leaves a
    durable record recovery can adjudicate instead of a silent maybe-sent.
    ``posted_at`` records that bytes reached the transport and nothing
    more; provider acceptance is a separate observation in a separate
    column, because a successful pane write is not acceptance and the two
    must never be readable as one fact.
    """

    __tablename__ = "claude_native_control_operations"

    operation_id = Column(Text, primary_key=True)
    kind = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    # The full binding this operation is valid for. A mismatch on any
    # component is refused before the pane is touched, so an operation
    # minted for one generation can never land in its successor.
    provider = Column(Text, nullable=False)
    native_session_id = Column(Text, nullable=False)
    terminal_id = Column(Text, nullable=False)
    generation = Column(Text, nullable=False)
    execution_mode = Column(Text, nullable=False)
    # Present only for a steer, which binds to the exact active turn it
    # intends to interrupt. A queue operation has no turn by definition.
    turn_id = Column(Text, nullable=True)
    # The digest of the exact literal payload rather than the payload
    # itself: enough to prove the same operation was not re-sent with
    # different bytes, without durably storing message content here.
    payload_sha256 = Column(Text, nullable=False)
    intent_json = Column(Text, nullable=False)
    transport_json = Column(Text, nullable=True)
    observation_json = Column(Text, nullable=True)
    posted_at = Column(Text, nullable=True)
    refusal_reason = Column(Text, nullable=True)
    ambiguity_reason = Column(Text, nullable=True)
    epoch = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


class CodexNativeControlOperationModel(Base):
    """One at-most-once control operation against a native Codex TUI.

    Kept in its own table so neither Claude nor Kimi operation evidence can
    satisfy a Codex delivery or ambiguity check.
    """

    __tablename__ = "codex_native_control_operations"

    operation_id = Column(Text, primary_key=True)
    kind = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    native_session_id = Column(Text, nullable=False)
    terminal_id = Column(Text, nullable=False)
    generation = Column(Text, nullable=False)
    execution_mode = Column(Text, nullable=False)
    turn_id = Column(Text, nullable=True)
    payload_sha256 = Column(Text, nullable=False)
    intent_json = Column(Text, nullable=False)
    transport_json = Column(Text, nullable=True)
    observation_json = Column(Text, nullable=True)
    posted_at = Column(Text, nullable=True)
    refusal_reason = Column(Text, nullable=True)
    ambiguity_reason = Column(Text, nullable=True)
    epoch = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)


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


# The v2 vintage surface (``managed_launch_v2_reservations``,
# ``managed_launch_v2_terminals``) is deliberately absent from the
# unconditional ``init_db`` create_all: it is created only by the gated
# transactional migration so a required exact-old-binary gate refusal
# precedes — and prevents — any v2 surface creation.
_V2_ORM_TABLE_NAMES = frozenset(
    {
        ManagedLaunchV2ReservationModel.__tablename__,
        ManagedLaunchV2TerminalModel.__tablename__,
    }
)


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
    # The v2 vintage surface is created ONLY by the gated transactional
    # migration (``_migrate_managed_launch_v2`` below): an exact-old-binary
    # gate refusal must abort initialization BEFORE any metadata operation
    # capable of creating v2 tables runs, so the v2 ORM models are excluded
    # from this unconditional create_all.
    Base.metadata.create_all(
        bind=engine,
        tables=[
            table for table in Base.metadata.sorted_tables if table.name not in _V2_ORM_TABLE_NAMES
        ],
    )
    _restrict_db_file_permissions()
    _migrate_terminals_schema()
    _migrate_memory_indexes()
    _migrate_add_access_count()
    _migrate_add_last_compiled_at()
    _migrate_add_related_keys()
    _migrate_workflow_index()
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    _migrate_session_env()
    _migrate_native_session_attachments()
    _migrate_kimi_native_control_operations()
    _migrate_claude_native_control_operations()
    _migrate_codex_native_control_operations()
    _migrate_managed_launch_reservations()
    _migrate_managed_launch_v2()


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
    except Exception as e:  # noqa: BLE001 — derived/recoverable; logged at debug (B4-RD-4)
        logger.debug(f"workflow_run_step migration skipped: {e}")


def _migrate_session_env() -> None:
    """Create the durable ``session_env`` table if missing (issue #248 durability).

    Persists the per-session forwarded env (``cao launch --env``) so it
    survives a cao-server restart. Idempotent (``CREATE TABLE IF NOT EXISTS``),
    zero-arg and self-connecting — mirrors ``_migrate_workflow_run``. Fresh DBs
    get the same DDL from ``Base.metadata.create_all`` via ``SessionEnvModel``;
    this covers DBs created before the model existed. Failure is logged at
    debug and never propagated — a missing table here is fail-closed at read
    time in ``services/session_env.get_session_env`` instead.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_env ("
                "session_name TEXT PRIMARY KEY, "
                "env_vars TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 — read path fails closed instead
        logger.debug(f"session_env migration skipped: {e}")


def _migrate_native_session_attachments() -> None:
    """Create the native-session attachment store on older databases.

    ``Base.metadata.create_all`` covers fresh databases via
    ``NativeSessionAttachmentModel``; this idempotent migration covers
    databases created before native execution mode existed. The DDL is
    byte-compatible with the ORM model so both paths yield one schema.

    Failure is surfaced rather than swallowed: every native attachment
    operation fails closed when this table cannot be read, and a native
    launch that cannot record exclusive ownership must never proceed to
    attach a provider session.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS native_session_attachments ("
                "provider TEXT NOT NULL, "
                "native_session_id TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "owner_terminal_id TEXT NOT NULL, "
                "owner_generation TEXT NOT NULL, "
                "owner_execution_mode TEXT NOT NULL, "
                "owner_pane_id TEXT, "
                "owner_process_identity_json TEXT, "
                "intent_json TEXT NOT NULL, "
                "release_proof_json TEXT, "
                "ambiguity_reason TEXT, "
                "epoch INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, "
                "PRIMARY KEY (provider, native_session_id)"
                ")"
            )
    except Exception as e:  # noqa: BLE001 - the operation path fails closed
        logger.warning(f"native-session attachment migration failed: {e}")


def _migrate_kimi_native_control_operations() -> None:
    """Create the native control-operation store on older databases.

    ``Base.metadata.create_all`` covers fresh databases via
    ``KimiNativeControlOperationModel``; this idempotent migration covers
    databases created before native control existed. The DDL is
    byte-compatible with the ORM model so both paths yield one schema.

    A failure here is logged rather than raised, matching the other
    migrations, because startup must not be blocked by a table that only
    one optional surface needs. Nothing unsafe follows from that: every
    native control operation fails closed when this table cannot be
    written, so an operation that cannot journal its intent types nothing
    into a provider pane.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kimi_native_control_operations ("
                "operation_id TEXT PRIMARY KEY, "
                "kind TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "provider TEXT NOT NULL, "
                "native_session_id TEXT NOT NULL, "
                "terminal_id TEXT NOT NULL, "
                "generation TEXT NOT NULL, "
                "execution_mode TEXT NOT NULL, "
                "turn_id TEXT, "
                "payload_sha256 TEXT NOT NULL, "
                "intent_json TEXT NOT NULL, "
                "transport_json TEXT, "
                "observation_json TEXT, "
                "posted_at TEXT, "
                "refusal_reason TEXT, "
                "ambiguity_reason TEXT, "
                "epoch INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 - the operation path fails closed
        logger.warning(f"kimi native control migration failed: {e}")


def _migrate_claude_native_control_operations() -> None:
    """Create the Claude control-operation store on older databases.

    The same idempotent, byte-compatible pattern as the Kimi store above,
    against its own table. A failure is logged rather than raised for the
    same reason: startup must not be blocked by a table only one optional
    surface needs, and nothing unsafe follows, because a control operation
    that cannot journal its intent types nothing into a provider pane.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS claude_native_control_operations ("
                "operation_id TEXT PRIMARY KEY, "
                "kind TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "provider TEXT NOT NULL, "
                "native_session_id TEXT NOT NULL, "
                "terminal_id TEXT NOT NULL, "
                "generation TEXT NOT NULL, "
                "execution_mode TEXT NOT NULL, "
                "turn_id TEXT, "
                "payload_sha256 TEXT NOT NULL, "
                "intent_json TEXT NOT NULL, "
                "transport_json TEXT, "
                "observation_json TEXT, "
                "posted_at TEXT, "
                "refusal_reason TEXT, "
                "ambiguity_reason TEXT, "
                "epoch INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 - the operation path fails closed
        logger.warning(f"claude native control migration failed: {e}")


def _migrate_codex_native_control_operations() -> None:
    """Create the provider-private Codex control journal on older databases."""
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS codex_native_control_operations ("
                "operation_id TEXT PRIMARY KEY, "
                "kind TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "provider TEXT NOT NULL, "
                "native_session_id TEXT NOT NULL, "
                "terminal_id TEXT NOT NULL, "
                "generation TEXT NOT NULL, "
                "execution_mode TEXT NOT NULL, "
                "turn_id TEXT, "
                "payload_sha256 TEXT NOT NULL, "
                "intent_json TEXT NOT NULL, "
                "transport_json TEXT, "
                "observation_json TEXT, "
                "posted_at TEXT, "
                "refusal_reason TEXT, "
                "ambiguity_reason TEXT, "
                "epoch INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 - operation paths fail closed
        logger.warning(f"codex native control migration failed: {e}")


def _migrate_managed_launch_reservations() -> None:
    """Create the response-loss-safe managed-launch store on older databases.

    ``Base.metadata.create_all`` covers fresh databases.  This explicit,
    idempotent migration is kept for installations whose database predates the
    companion protocol.  Unlike best-effort derived indexes, failure here is
    surfaced by every managed-launch operation when the required table cannot
    be read; callers never fall back to ordinary terminal creation.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    try:
        with sqlite3.connect(str(DATABASE_FILE)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS managed_launch_reservations ("
                "reservation_id TEXT PRIMARY KEY, "
                "terminal_id TEXT NOT NULL UNIQUE, "
                "generation TEXT NOT NULL UNIQUE, "
                "session_name TEXT NOT NULL, "
                "provider TEXT NOT NULL, "
                "agent_profile TEXT NOT NULL, "
                "caller_id TEXT NOT NULL, "
                "working_directory TEXT NOT NULL, "
                "trusted_project_root TEXT, "
                "state TEXT NOT NULL, "
                "request_json TEXT NOT NULL, "
                "observations_json TEXT NOT NULL DEFAULT '[]', "
                "readiness_json TEXT, "
                "admission_json TEXT, "
                "negative_json TEXT, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
    except Exception as e:  # noqa: BLE001 - the operation path fails closed
        logger.warning(f"managed-launch migration failed: {e}")


def _migrate_managed_launch_v2() -> None:
    """Create the isolated managed-launch v2 store on older databases.

    The v2 table is a separate surface: every pre-existing row in any v1
    table is classified immutable v1 by its absence here, and no v1
    reader or deleter can see v2 rows.  ``protocol_vintage`` is pinned to
    'v2' at the DDL level so a v2 row can never silently downgrade.  The
    real transactional migration (with journaled rollback/drain) lives in
    ``services/vintage_migration.py``; this delegates to it.  When
    ``CAO_OLD_BINARY_GATE=require`` is configured, the exact-old-binary
    invisibility proof (H_B's actual entrypoints against v2 forward state)
    runs as the rollout gate FIRST — before any v2-capable metadata
    operation (the v2 ORM models are excluded from ``init_db``'s
    unconditional create_all) — and a refusal or rig failure ABORTS
    initialization by propagating: a configured rollout may never proceed
    on the prohibited surface, so the refusal is never logged and
    swallowed.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE
    from cli_agent_orchestrator.services import vintage_migration

    gate = vintage_migration.configured_old_binary_gate()
    if gate is not None:
        # Required gate: run it (inside migrate_v2, before any v2 DDL) and
        # let OldBinaryGateRefused / rig failures propagate to the caller.
        vintage_migration.migrate_v2(DATABASE_FILE, old_binary_gate=gate)
        return
    try:
        vintage_migration.migrate_v2(DATABASE_FILE, old_binary_gate=None)
    except Exception as e:  # noqa: BLE001 - the operation path fails closed
        logger.warning(f"managed-launch v2 migration failed: {e}")


def _migrate_terminals_schema() -> None:
    """Add allowed_tools and shell_command columns to terminals table if missing (schema migration)."""
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
        if "generation" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN generation TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_terminals_generation "
                "ON terminals(generation) WHERE generation IS NOT NULL"
            )
            conn.commit()
            logger.info("Migration: added generation column to terminals table")
        if "pane_id" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN pane_id TEXT")
            conn.commit()
            logger.info("Migration: added pane_id column to terminals table")
        if "window_id" not in columns:
            conn.execute("ALTER TABLE terminals ADD COLUMN window_id TEXT")
            conn.commit()
            logger.info("Migration: added window_id column to terminals table")
        if "server_socket_path" not in columns:
            # Added NULL for every existing row, and deliberately not
            # backfilled (§24.7). Backfilling from the server this process
            # happens to be talking to would bind each legacy terminal to
            # whichever tmux server ran the migration — which is exactly
            # the mistake the column exists to catch. A legacy row stays
            # unbound and refuses until something re-observes it.
            conn.execute("ALTER TABLE terminals ADD COLUMN server_socket_path TEXT")
            conn.commit()
            logger.info("Migration: added server_socket_path column to terminals table")
        # The rest of the canonical identity tuple, and the lifecycle that
        # lets a row stop claiming to be a terminal once its pane is gone.
        # Every one of these lands NULL on existing rows and none is
        # backfilled, for the same reason server_socket_path is not: the
        # process running the migration would be inventing the facts from
        # whatever tmux server it happens to reach. A NULL lifecycle_state
        # therefore reads as "never observed", which the projection reports
        # as unknown liveness rather than promoting to live.
        for column, ddl in (
            ("session_id", "ALTER TABLE terminals ADD COLUMN session_id TEXT"),
            ("pane_pid", "ALTER TABLE terminals ADD COLUMN pane_pid INTEGER"),
            ("native_session_id", "ALTER TABLE terminals ADD COLUMN native_session_id TEXT"),
            ("lifecycle_state", "ALTER TABLE terminals ADD COLUMN lifecycle_state TEXT"),
            ("lifecycle_reason", "ALTER TABLE terminals ADD COLUMN lifecycle_reason TEXT"),
            (
                "liveness_checked_at",
                "ALTER TABLE terminals ADD COLUMN liveness_checked_at DATETIME",
            ),
            (
                "superseded_by_terminal_id",
                "ALTER TABLE terminals ADD COLUMN superseded_by_terminal_id TEXT",
            ),
            (
                "superseded_by_generation",
                "ALTER TABLE terminals ADD COLUMN superseded_by_generation TEXT",
            ),
        ):
            if column not in columns:
                conn.execute(ddl)
                conn.commit()
                logger.info("Migration: added %s column to terminals table", column)
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
    generation: Optional[str] = None,
    pane_id: Optional[str] = None,
    window_id: Optional[str] = None,
    server_socket_path: Optional[str] = None,
    session_id: Optional[str] = None,
    pane_pid: Optional[int] = None,
    native_session_id: Optional[str] = None,
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
            shell_command=shell_command,
            caller_id=caller_id,
            generation=generation,
            pane_id=pane_id,
            window_id=window_id,
            server_socket_path=server_socket_path,
            session_id=session_id,
            pane_pid=pane_pid,
            native_session_id=native_session_id,
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
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
            "generation": terminal.generation,
            "pane_id": terminal.pane_id,
            "window_id": terminal.window_id,
            "server_socket_path": terminal.server_socket_path,
            "session_id": terminal.session_id,
            "pane_pid": terminal.pane_pid,
            "native_session_id": terminal.native_session_id,
        }


def create_terminal_v2(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    caller_id: Optional[str] = None,
    generation: Optional[str] = None,
    pane_id: Optional[str] = None,
    window_id: Optional[str] = None,
    server_socket_path: Optional[str] = None,
    session_id: Optional[str] = None,
    pane_pid: Optional[int] = None,
    native_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a v2 managed terminal metadata record (isolated vintage surface).

    v2 managed terminals live ONLY in ``managed_launch_v2_terminals`` —
    never in the shared ``terminals`` table — so old-binary query, list,
    watchdog, and cleanup paths have zero visibility into them.
    """
    import json as _json

    if not generation:
        raise ValueError("v2 managed terminals require the exact generation")
    with SessionLocal() as db:
        terminal = ManagedLaunchV2TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile,
            allowed_tools=_json.dumps(allowed_tools) if allowed_tools else None,
            caller_id=caller_id,
            generation=generation,
            protocol_vintage="v2",
            pane_id=pane_id,
            window_id=window_id,
            server_socket_path=server_socket_path,
            v2_session_id=session_id,
            v2_pane_pid=pane_pid,
            v2_native_session_id=native_session_id,
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
            "caller_id": terminal.caller_id,
            "generation": terminal.generation,
            "protocol_vintage": "v2",
            "pane_id": terminal.pane_id,
            "window_id": terminal.window_id,
            "server_socket_path": terminal.server_socket_path,
            "v2_session_id": terminal.v2_session_id,
            "v2_pane_pid": terminal.v2_pane_pid,
            "v2_native_session_id": terminal.v2_native_session_id,
        }


def get_terminal_metadata_v2(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Get v2 managed terminal metadata by ID (isolated vintage surface)."""
    import json as _json

    with SessionLocal() as db:
        terminal = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(ManagedLaunchV2TerminalModel.id == terminal_id)
            .first()
        )
        if not terminal:
            return None
        allowed_tools = _json.loads(terminal.allowed_tools) if terminal.allowed_tools else None
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "allowed_tools": allowed_tools,
            "shell_command": None,
            "caller_id": terminal.caller_id,
            "generation": terminal.generation,
            "protocol_vintage": "v2",
            "pane_id": terminal.pane_id,
            "window_id": terminal.window_id,
            "server_socket_path": terminal.server_socket_path,
            "v2_session_id": terminal.v2_session_id,
            "v2_pane_pid": terminal.v2_pane_pid,
            "v2_native_session_id": terminal.v2_native_session_id,
            "v2_lifecycle_state": terminal.v2_lifecycle_state,
            "v2_lifecycle_reason": terminal.v2_lifecycle_reason,
            "v2_liveness_checked_at": terminal.v2_liveness_checked_at,
            "v2_superseded_by_terminal_id": terminal.v2_superseded_by_terminal_id,
            "v2_superseded_by_generation": terminal.v2_superseded_by_generation,
            "last_active": terminal.last_active,
        }


def delete_terminal_v2(terminal_id: str) -> bool:
    """Delete a v2 managed terminal metadata record (v2 surface only)."""
    with SessionLocal() as db:
        deleted = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(ManagedLaunchV2TerminalModel.id == terminal_id)
            .delete()
        )
        db.commit()
        return deleted > 0


def delete_terminal_v2_if_generation(terminal_id: str, generation: str) -> bool:
    """Delete a v2 managed terminal row only if it still names the generation."""
    with SessionLocal() as db:
        deleted = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(
                ManagedLaunchV2TerminalModel.id == terminal_id,
                ManagedLaunchV2TerminalModel.generation == generation,
            )
            .delete()
        )
        db.commit()
        return deleted > 0


def record_v2_cleanup_first_writer(
    reservation_id: str,
    *,
    build_record: Any,
    terminal_id: str,
    generation: str,
) -> bool:
    """Delete the v2 terminal row and record the cleanup proof, atomically.

    ``build_record`` is called with the observed ``terminal_record_removed``
    and returns the canonical proof bytes to store, so the value recorded is
    the one this transaction actually observed rather than one the caller
    guessed before the delete.

    Returns True when this call was the first writer and both effects
    landed, False when another writer already recorded a cleanup for this
    reservation — in which case nothing here is written and no row is
    deleted.

    The two effects share one transaction because the proof states what
    *this* call observed. Deleting first and recording second would leave a
    crash window in which the row is gone and no record says so: the retry
    would then observe an already-absent row and record
    ``terminal_record_removed: false``, permanently attributing the removal
    to nobody. Recording first and deleting second has the mirror problem —
    a proof claiming a removal that never happened. Rolling both back
    together is the only ordering where the proof cannot describe a world
    that did not occur.

    The write condition is ``cleanup_json IS NULL``, which is the
    first-writer-wins rule stated where it is enforced rather than checked
    beforehand and hoped for: two concurrent cleanups both read no record,
    and exactly one of them commits.
    """
    with SessionLocal() as db:
        deleted = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(
                ManagedLaunchV2TerminalModel.id == terminal_id,
                ManagedLaunchV2TerminalModel.generation == generation,
            )
            .delete(synchronize_session=False)
        )
        updated = (
            db.query(ManagedLaunchV2ReservationModel)
            .filter(
                ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                ManagedLaunchV2ReservationModel.cleanup_json.is_(None),
            )
            .update(
                {
                    "cleanup_json": build_record(deleted > 0),
                    "updated_at": datetime.now().isoformat(),
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            # Lost the race. The rollback also undoes the delete above, so
            # the winner's recorded ``terminal_record_removed`` stays the
            # only account of what happened to the row.
            db.rollback()
            return False
        db.commit()
        return True


def v2_cleanup_record(reservation_id: str) -> Optional[str]:
    """The durable cleanup proof for a reservation, or None."""
    with SessionLocal() as db:
        row = (
            db.query(ManagedLaunchV2ReservationModel)
            .filter(ManagedLaunchV2ReservationModel.reservation_id == reservation_id)
            .first()
        )
        return None if row is None else row.cleanup_json


# COND-0242: `get_terminal_metadata` is called from hot paths (status
# projection, output reads, inbox delivery, recovery consideration), so a
# terminal whose row is gone produced one WARNING per call. In production
# several such terminals at once turned that into a sustained warning storm
# that amplified an unrelated failure into log pressure and buried the one
# error that mattered. The first observation per terminal stays loud; only the
# repeats are rate-limited, and a terminal that comes back clears its entry so
# a later disappearance is reported again.
_MISSING_TERMINAL_LOG_INTERVAL_S = 300.0
# A hard cap, not a prune threshold. Dropping only entries already older than
# the interval is no bound at all: more than this many distinct ids missing
# inside one interval leaves nothing stale to drop, so the table grows without
# limit and every later report pays an O(n) scan under the lock — on a hot
# lookup path. Evicting the oldest insertion once full keeps the table at
# exactly this size. Losing the oldest entry only costs one extra WARNING for
# an id that has not been seen in a while, which is the right thing to spend.
_MISSING_TERMINAL_LOG_CAPACITY = 512
# Insertion-ordered, so the first key is always the oldest recorded report.
_missing_terminal_logged_at: "OrderedDict[str, float]" = OrderedDict()
_missing_terminal_log_lock = threading.Lock()


def _should_report_missing_terminal(terminal_id: str) -> bool:
    now = time.monotonic()
    with _missing_terminal_log_lock:
        last = _missing_terminal_logged_at.get(terminal_id)
        if last is not None and now - last < _MISSING_TERMINAL_LOG_INTERVAL_S:
            return False
        # Re-report: drop the stale entry so it re-enters at the newest end
        # and cannot be evicted ahead of ids reported longer ago.
        _missing_terminal_logged_at.pop(terminal_id, None)
        while len(_missing_terminal_logged_at) >= _MISSING_TERMINAL_LOG_CAPACITY:
            _missing_terminal_logged_at.popitem(last=False)
        _missing_terminal_logged_at[terminal_id] = now
        return True


def report_terminal_missing_from_every_store(terminal_id: str) -> None:
    """Report a terminal that no store could resolve, rate-limited per terminal.

    The counterpart to ``warn_if_missing=False``: a two-tier resolver silences
    its v1 probe and calls this only once every tier has missed, so the warning
    keeps meaning what it says.
    """
    if _should_report_missing_terminal(terminal_id):
        logger.warning(
            f"Terminal metadata not found for terminal_id: {terminal_id} "
            f"(repeats suppressed for {_MISSING_TERMINAL_LOG_INTERVAL_S:.0f}s)"
        )


def get_terminal_metadata(
    terminal_id: str, *, warn_if_missing: bool = True
) -> Optional[Dict[str, Any]]:
    """Get terminal metadata by ID.

    ``warn_if_missing=False`` is for callers that use this as the FIRST TIER of
    a two-tier v1-then-v2 probe. For a healthy v2-only terminal the v1 miss is
    the expected outcome, not a fault, and the clear-on-success path below can
    never fire for it — the v1 lookup never succeeds — so warning here produced
    a permanent recurring false alarm about a live terminal, on the exact log
    surface COND-0242 exists to keep readable. Such callers report the miss
    themselves, once, only when BOTH tiers come up empty.
    """
    import json as _json

    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            if warn_if_missing and _should_report_missing_terminal(terminal_id):
                logger.warning(
                    f"Terminal metadata not found for terminal_id: {terminal_id} "
                    f"(repeats suppressed for {_MISSING_TERMINAL_LOG_INTERVAL_S:.0f}s)"
                )
            return None
        if _missing_terminal_logged_at:
            with _missing_terminal_log_lock:
                _missing_terminal_logged_at.pop(terminal_id, None)
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
            "shell_command": terminal.shell_command,
            "caller_id": terminal.caller_id,
            "generation": terminal.generation,
            "pane_id": terminal.pane_id,
            "window_id": terminal.window_id,
            "server_socket_path": terminal.server_socket_path,
            "session_id": terminal.session_id,
            "pane_pid": terminal.pane_pid,
            "native_session_id": terminal.native_session_id,
            "lifecycle_state": terminal.lifecycle_state,
            "lifecycle_reason": terminal.lifecycle_reason,
            "liveness_checked_at": terminal.liveness_checked_at,
            "superseded_by_terminal_id": terminal.superseded_by_terminal_id,
            "superseded_by_generation": terminal.superseded_by_generation,
            "last_active": terminal.last_active,
        }


#: Why a live-incarnation registration did not happen. A closed vocabulary
#: rather than a boolean because the three failures need different fixes
#: and one flag makes them indistinguishable exactly when somebody has to
#: know which occurred: an absent row means the launch wrote no terminal, a
#: partial identity means the pane could not be fully observed, and a held
#: pane means something tried to move an existing handle. The last is a
#: safety refusal and the other two are launch faults, so a caller that
#: cannot tell them apart cannot fail closed correctly either.
REGISTRATION_OK = "registered"
REGISTRATION_ABSENT_ROW = "absent-row"
REGISTRATION_PARTIAL_IDENTITY = "partial-identity"
REGISTRATION_PANE_ALREADY_HELD = "pane-already-held"
REGISTRATION_GENERATION_MISMATCH = "generation-mismatch"

REGISTRATION_OUTCOMES = frozenset(
    {
        REGISTRATION_OK,
        REGISTRATION_ABSENT_ROW,
        REGISTRATION_PARTIAL_IDENTITY,
        REGISTRATION_PANE_ALREADY_HELD,
        REGISTRATION_GENERATION_MISMATCH,
    }
)


def register_terminal_incarnation(
    terminal_id: str,
    *,
    generation: Optional[str],
    server_socket_path: str,
    session_id: str,
    window_id: str,
    pane_id: str,
    pane_pid: int,
    native_session_id: Optional[str] = None,
) -> bool:
    """Bool form of :func:`register_terminal_incarnation_outcome`.

    Kept because ``True``/``False`` is what the v1 launch path has always
    asked for. It is a projection of the typed outcome, never a second
    implementation of the rules.
    """
    return (
        register_terminal_incarnation_outcome(
            terminal_id,
            generation=generation,
            server_socket_path=server_socket_path,
            session_id=session_id,
            window_id=window_id,
            pane_id=pane_id,
            pane_pid=pane_pid,
            native_session_id=native_session_id,
        )
        == REGISTRATION_OK
    )


def register_terminal_incarnation_outcome(
    terminal_id: str,
    *,
    generation: Optional[str],
    server_socket_path: str,
    session_id: str,
    window_id: str,
    pane_id: str,
    pane_pid: int,
    native_session_id: Optional[str] = None,
) -> str:
    """Write one live incarnation's complete canonical identity in a single
    transaction, and mark it live.

    Returns ``REGISTRATION_OK`` when the row now carries exactly this
    tuple — whether this call wrote it or an identical earlier call
    already had — and otherwise the exact reason it does not.

    Two properties the callers depend on:

    Idempotent by ``(terminal_id, generation)``. Re-driving a registration
    is how recovery works, so it has to be free: the second call observes
    that every field already matches and writes nothing.

    Never re-points. A row already registered to a different pane is left
    exactly as it is and the call fails. Overwriting it would silently move
    a handle from the pane somebody registered onto a pane they did not,
    which is the aliasing that makes a stale row dangerous. A genuinely new
    incarnation gets a new row with a fresh generation and points the old
    one at it through :func:`supersede_terminal`.
    """
    if not (server_socket_path and session_id and window_id and pane_id) or pane_pid <= 0:
        # Partial identity is refused outright rather than stored with the
        # unreadable parts left NULL: a row published with three of five
        # fields is a row some later check will pass against.
        return REGISTRATION_PARTIAL_IDENTITY
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal is None:
            return REGISTRATION_ABSENT_ROW
        already = (
            terminal.pane_id is not None
            or terminal.window_id is not None
            or terminal.session_id is not None
        )
        matches = (
            terminal.pane_id == pane_id
            and terminal.window_id == window_id
            and terminal.session_id == session_id
            and terminal.pane_pid == pane_pid
            and terminal.server_socket_path == server_socket_path
        )
        if already and not matches:
            logger.warning(
                "Refusing to re-point terminal %s from pane %s to %s",
                terminal_id,
                terminal.pane_id,
                pane_id,
            )
            return REGISTRATION_PANE_ALREADY_HELD
        if generation is not None and terminal.generation not in (None, generation):
            logger.warning(
                "Refusing to register terminal %s under generation %s; row holds %s",
                terminal_id,
                generation,
                terminal.generation,
            )
            return REGISTRATION_GENERATION_MISMATCH
        terminal.server_socket_path = server_socket_path
        terminal.session_id = session_id
        terminal.window_id = window_id
        terminal.pane_id = pane_id
        terminal.pane_pid = pane_pid
        if generation is not None:
            terminal.generation = generation
        if native_session_id is not None:
            terminal.native_session_id = native_session_id
        terminal.lifecycle_state = "live"
        terminal.lifecycle_reason = None
        terminal.liveness_checked_at = datetime.now()
        terminal.superseded_by_terminal_id = None
        terminal.superseded_by_generation = None
        db.commit()
        return REGISTRATION_OK


def register_v2_terminal_incarnation_outcome(
    terminal_id: str,
    *,
    generation: Optional[str],
    server_socket_path: str,
    session_id: str,
    window_id: str,
    pane_id: str,
    pane_pid: int,
    native_session_id: Optional[str] = None,
) -> str:
    """The v2 twin of :func:`register_terminal_incarnation_outcome`.

    Same rules, same vocabulary, different store. A v2 managed terminal
    lives only in ``managed_launch_v2_terminals``, so the legacy writer
    asking ``terminals`` for it found nothing and reported an absent row
    for every native launch — a false negative that read as a bookkeeping
    hiccup and was logged and passed over, leaving a live pane with no
    registered incarnation.

    Duplicated rather than shared because the two stores use different
    column names for the same facts (the v2 identity columns are
    ``v2_``-prefixed so the vintage receipt's bare names stay unique), and
    a single writer switching column names by vintage would be one edit
    away from writing v2 identity into the legacy row. The v2 row is never
    copied into ``terminals``: vintage isolation is what gives an old
    binary zero visibility into v2 state, and a status lookup is not worth
    spending it.
    """
    if not (server_socket_path and session_id and window_id and pane_id) or pane_pid <= 0:
        return REGISTRATION_PARTIAL_IDENTITY
    with SessionLocal() as db:
        terminal = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(ManagedLaunchV2TerminalModel.id == terminal_id)
            .first()
        )
        if terminal is None:
            return REGISTRATION_ABSENT_ROW
        already = (
            terminal.pane_id is not None
            or terminal.window_id is not None
            or terminal.v2_session_id is not None
        )
        matches = (
            terminal.pane_id == pane_id
            and terminal.window_id == window_id
            and terminal.v2_session_id == session_id
            and terminal.v2_pane_pid == pane_pid
            and terminal.server_socket_path == server_socket_path
        )
        if already and not matches:
            logger.warning(
                "Refusing to re-point v2 terminal %s from pane %s to %s",
                terminal_id,
                terminal.pane_id,
                pane_id,
            )
            return REGISTRATION_PANE_ALREADY_HELD
        if generation is not None and terminal.generation not in (None, generation):
            logger.warning(
                "Refusing to register v2 terminal %s under generation %s; row holds %s",
                terminal_id,
                generation,
                terminal.generation,
            )
            return REGISTRATION_GENERATION_MISMATCH
        terminal.server_socket_path = server_socket_path
        terminal.v2_session_id = session_id
        terminal.window_id = window_id
        terminal.pane_id = pane_id
        terminal.v2_pane_pid = pane_pid
        if native_session_id is not None:
            terminal.v2_native_session_id = native_session_id
        terminal.v2_lifecycle_state = "live"
        terminal.v2_lifecycle_reason = None
        terminal.v2_liveness_checked_at = datetime.now().isoformat()
        terminal.v2_superseded_by_terminal_id = None
        terminal.v2_superseded_by_generation = None
        db.commit()
        return REGISTRATION_OK


def refresh_terminal_window_names(
    terminal_id: str,
    *,
    tmux_session: str,
    tmux_window: str,
    pane_id: str,
) -> bool:
    """Update a terminal's mutable tmux labels to what its own pane reports.

    Guarded on ``pane_id`` so this can only ever relabel the row whose
    identity the caller has just verified. The labels are the only things
    that change: a rename moves a window's name, not the window.

    Callers must have proven the identity first. Without that proof this
    would be indistinguishable from re-pointing a row at whatever currently
    wears the name — the exact aliasing the identity boundary exists to
    prevent — so the pane-id filter is the safety property, not an
    optimisation.
    """
    if not pane_id:
        return False
    with SessionLocal() as db:
        updated = (
            db.query(TerminalModel)
            .filter(TerminalModel.id == terminal_id, TerminalModel.pane_id == pane_id)
            .update(
                {"tmux_session": tmux_session, "tmux_window": tmux_window},
                synchronize_session=False,
            )
        )
        db.commit()
        return updated == 1


def upgrade_terminal_identity_from_observation(
    terminal_id: str,
    *,
    pane_id: str,
    server_socket_path: str,
    session_id: str,
    pane_pid: int,
    native_session_id: Optional[str] = None,
) -> bool:
    """Complete a row that predates the two newest identity fields.

    The build deployed before this one wrote ``pane_id``, ``window_id`` and
    ``server_socket_path`` and nothing else, so every row it created grades
    partial once ``session_id`` and ``pane_pid`` join the canonical tuple —
    and partial fails closed. Without this, installing over an existing
    database would leave the whole running fleet unable to take control
    input, unreadable, and unattachable.

    What makes this safe is the same thing that makes the migration right
    to refuse a blind backfill: the values written here come from
    *observing the row's own recorded pane on the row's own recorded
    socket*. The migration cannot do that — it runs against whichever
    server the upgrading process happens to reach, and recording that as
    the terminal's binding would make the later identity check confirm the
    migration's guess. An observation of the named pane on the named socket
    confirms nothing; it reports.

    Guarded so it can only ever complete, never re-point:

    * the update matches on the row's existing ``pane_id`` **and**
      ``server_socket_path``, so a row whose pane moved is not touched;
    * it matches on ``session_id IS NULL AND pane_pid IS NULL``, so a
      complete row is never rewritten and two concurrent upgrades cannot
      both win;
    * a row with no ``pane_id`` or no ``server_socket_path`` is out of
      scope entirely — there is nothing to observe it against, and that is
      exactly the case where guessing would be wrong.

    Returns True only when this call completed the row.
    """
    if not (pane_id and server_socket_path and session_id) or pane_pid <= 0:
        return False
    with SessionLocal() as db:
        values: Dict[str, Any] = {
            "session_id": session_id,
            "pane_pid": pane_pid,
        }
        if native_session_id is not None:
            values["native_session_id"] = native_session_id
        updated = (
            db.query(TerminalModel)
            .filter(
                TerminalModel.id == terminal_id,
                TerminalModel.pane_id == pane_id,
                TerminalModel.server_socket_path == server_socket_path,
                TerminalModel.session_id.is_(None),
                TerminalModel.pane_pid.is_(None),
            )
            .update(values, synchronize_session=False)
        )
        db.commit()
        return updated == 1


def upgrade_v2_terminal_identity_from_observation(
    terminal_id: str,
    *,
    pane_id: str,
    server_socket_path: str,
    session_id: str,
    pane_pid: int,
) -> bool:
    """The v2 twin of :func:`upgrade_terminal_identity_from_observation`.

    The managed store carries the same three-of-five rows for the same
    reason: the deployed build wrote ``pane_id``, ``window_id`` and
    ``server_socket_path`` into ``managed_launch_v2_terminals`` too. With
    only the shared-table writer, projecting a managed row matches zero
    rows every time, the upgrade never lands, and the whole preserved
    managed fleet is graded ``unknown-liveness`` permanently — the
    fail-closed outcome, applied to rows that a single observation could
    have answered.

    Written as a separate function rather than a vintage argument on one
    writer, for the same reason the native-session setters are split: the
    two stores are isolated by design, and a single writer choosing its
    table at runtime is one bug away from a v1 path touching a v2 row.

    The guards are identical, on the v2 columns:

    * matched on the row's existing ``pane_id`` **and**
      ``server_socket_path``, so a row whose pane moved is not touched;
    * matched on ``v2_session_id IS NULL AND v2_pane_pid IS NULL``, so an
      already-complete row is never rewritten and two concurrent upgrades
      cannot both win;
    * a row with no pane or no socket is out of scope — there is nothing
      to observe it against.

    Returns True only when this call completed the row.
    """
    if not (pane_id and server_socket_path and session_id) or pane_pid <= 0:
        return False
    with SessionLocal() as db:
        updated = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(
                ManagedLaunchV2TerminalModel.id == terminal_id,
                ManagedLaunchV2TerminalModel.pane_id == pane_id,
                ManagedLaunchV2TerminalModel.server_socket_path == server_socket_path,
                ManagedLaunchV2TerminalModel.v2_session_id.is_(None),
                ManagedLaunchV2TerminalModel.v2_pane_pid.is_(None),
            )
            .update(
                {"v2_session_id": session_id, "v2_pane_pid": pane_pid},
                synchronize_session=False,
            )
        )
        db.commit()
        return updated == 1


def set_terminal_native_session_id(terminal_id: str, native_session_id: str) -> bool:
    """Record the provider-native session this terminal's pane is running.

    Separate from :func:`register_terminal_incarnation` because the two
    facts are learned at different moments: the pane identity exists as
    soon as the window does, while the native session id is only proven
    once the provider has answered — by its SessionStart hook for Claude,
    or by the ACP bootstrap for Kimi. Writing it early, from the value the
    launcher *intended* to use, would record an assertion rather than an
    observation.

    Refuses to re-point: a row already carrying a different native session
    is left alone, because a pane that is running someone else's session is
    a supersession, not an update.
    """
    if not native_session_id:
        return False
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal is None:
            return False
        if terminal.native_session_id not in (None, native_session_id):
            logger.warning(
                "Refusing to re-point terminal %s from native session %s to %s",
                terminal_id,
                terminal.native_session_id,
                native_session_id,
            )
            return False
        terminal.native_session_id = native_session_id
        db.commit()
        return True


def set_terminal_v2_native_session_id(terminal_id: str, native_session_id: str) -> bool:
    """The v2 twin of :func:`set_terminal_native_session_id`.

    Separate rather than a vintage argument on one function, because the
    two stores are isolated by design and a single writer that chose its
    table at runtime would be one bug away from a v1 path touching a v2
    row. Same rule: idempotent for the same session, refused for a
    different one, since a pane running someone else's session is a
    supersession rather than an update.
    """
    if not native_session_id:
        return False
    with SessionLocal() as db:
        terminal = (
            db.query(ManagedLaunchV2TerminalModel)
            .filter(ManagedLaunchV2TerminalModel.id == terminal_id)
            .first()
        )
        if terminal is None:
            return False
        if terminal.v2_native_session_id not in (None, native_session_id):
            logger.warning(
                "Refusing to re-point v2 terminal %s from native session %s to %s",
                terminal_id,
                terminal.v2_native_session_id,
                native_session_id,
            )
            return False
        terminal.v2_native_session_id = native_session_id
        db.commit()
        return True


def find_terminal_by_pane_identity(
    *,
    server_socket_path: str,
    pane_id: str,
    session_id: Optional[str] = None,
    pane_pid: Optional[int] = None,
    exclude_terminal_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The terminal registered to this exact pane, if any row claims it.

    Used to name a successor when an older row is demoted: the demotion
    already knows *which pane* it observed and that the pane is no longer
    the incarnation that registered it, and this answers "so whose is it
    now?". Without it a superseded row can only say that it lost its pane,
    which leaves an operator and the conductor with nothing to act on.

    Matched on the full observed identity rather than the pane id alone,
    because a pane id is unique only within one server and this lookup runs
    precisely when ids are known to have been reissued.
    """
    if not (server_socket_path and pane_id):
        return None
    with SessionLocal() as db:
        query = db.query(TerminalModel).filter(
            TerminalModel.server_socket_path == server_socket_path,
            TerminalModel.pane_id == pane_id,
        )
        if session_id is not None:
            query = query.filter(TerminalModel.session_id == session_id)
        if pane_pid is not None:
            query = query.filter(TerminalModel.pane_pid == pane_pid)
        if exclude_terminal_id is not None:
            query = query.filter(TerminalModel.id != exclude_terminal_id)
        row = query.first()
        if row is None:
            return None
        return {"terminal_id": row.id, "generation": row.generation}


def record_terminal_lifecycle(
    terminal_id: str,
    *,
    state: str,
    reason: Optional[str] = None,
    superseded_by_terminal_id: Optional[str] = None,
    superseded_by_generation: Optional[str] = None,
) -> bool:
    """Record the outcome of one liveness observation against a terminal.

    The row's *identity* is never touched here — only what was observed
    about it. A demotion says "this identity no longer resolves to what was
    registered"; it does not get to decide what the identity now is.
    """
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal is None:
            return False
        terminal.lifecycle_state = state
        terminal.lifecycle_reason = reason
        terminal.liveness_checked_at = datetime.now()
        terminal.superseded_by_terminal_id = superseded_by_terminal_id
        terminal.superseded_by_generation = superseded_by_generation
        db.commit()
        return True


def backfill_terminal_identity_if_missing(terminal_id: str, pane_id: str, window_id: str) -> bool:
    """Write both legacy identity fields once, only while both remain null.

    ``server_socket_path`` is deliberately NOT backfilled here (§24.7).
    This runs against whichever tmux server the calling process reaches,
    and recording that server as the terminal's binding would make the
    writer-boundary check agree with whatever the backfill happened to
    see — the check would confirm its own guess. A row backfilled by this
    path therefore carries a pane id and no server, and refuses control
    input until something that actually knows the server records one.
    """
    if not pane_id or not window_id:
        return False
    with SessionLocal() as db:
        updated = (
            db.query(TerminalModel)
            .filter(
                TerminalModel.id == terminal_id,
                TerminalModel.pane_id.is_(None),
                TerminalModel.window_id.is_(None),
            )
            .update(
                {
                    TerminalModel.pane_id: pane_id,
                    TerminalModel.window_id: window_id,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return updated == 1


def list_terminals_by_session(tmux_session: str) -> List[Dict[str, Any]]:
    """List all terminals in a tmux session."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).all()
        result = [
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
        try:
            managed = (
                db.query(ManagedLaunchV2TerminalModel)
                .filter(ManagedLaunchV2TerminalModel.tmux_session == tmux_session)
                .all()
            )
        except OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            managed = []
        result.extend(
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "last_active": t.last_active,
                "generation": t.generation,
                "protocol_vintage": "v2",
            }
            for t in managed
        )
        return result


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


def _live_managed_v2_terminal_clauses():
    """The one shared current/live/non-superseded managed-v2 receiver predicate.

    Mirrors the registration writer (``register_terminal_incarnation_outcome_v2``
    sets ``live`` and clears both supersession pointers), the supersede path
    (which sets the pointers), and collection (which deletes the row). Used by
    both cross-vintage inbox query sites so they can never drift apart.
    """
    return (
        ManagedLaunchV2TerminalModel.v2_lifecycle_state == "live",
        ManagedLaunchV2TerminalModel.v2_superseded_by_terminal_id.is_(None),
        ManagedLaunchV2TerminalModel.v2_superseded_by_generation.is_(None),
    )


def _inbox_receiver_eligible(db, receiver_id: str) -> bool:
    """Cross-vintage inbox receiver eligibility for ordinary inbox creation.

    v1: a ``terminals`` row with the exact id is eligible (unchanged).
    v2: a ``managed_launch_v2_terminals`` row is eligible only while it is the
    current live incarnation — ``_live_managed_v2_terminal_clauses``. An id
    present in BOTH vintages refuses as ambiguous, mirroring
    ``managed_control_identity``'s ``ManagedLaunchConflict``. A pre-v2 schema
    has no v2 table: the ``OperationalError`` guard treats the v2 surface as
    absent, keeping v1 behavior bit-identical. A v2 identity is NEVER copied
    into ``terminals``.
    """
    v1_exists = db.query(TerminalModel).filter(TerminalModel.id == receiver_id).first() is not None
    try:
        v2_query = db.query(ManagedLaunchV2TerminalModel).filter(
            ManagedLaunchV2TerminalModel.id == receiver_id
        )
        v2_present = v2_query.first() is not None
        v2_live = v2_query.filter(*_live_managed_v2_terminal_clauses()).first() is not None
    except OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        v2_present = False
        v2_live = False
    if v1_exists and v2_present:
        raise ValueError(
            f"ambiguous managed terminal identity across protocol vintages: {receiver_id}"
        )
    return v1_exists or v2_live


def list_pending_receiver_ids_older_than(min_age_seconds: int) -> List[str]:
    """List receiver terminal IDs whose messages have been PENDING too long.

    Returns the distinct receivers of any message still PENDING for longer than
    ``min_age_seconds``. Used by the inbox reconciliation sweep to find messages
    the immediate and watchdog delivery paths missed, without competing with
    them for freshly queued ones (issue #131).

    The join on ``terminals`` drops messages whose receiver terminal no longer
    exists, so the sweep does not keep retrying deliveries to deleted agents.
    Managed-v2 receivers never appear in ``terminals``; the v2 branch adopts
    their stale PENDING rows under the same live/non-superseded predicate inbox
    creation enforces, so collected, superseded, or non-live v2 receivers stay
    excluded and a server bounce cannot strand a live v2 receiver's row. The
    result is the distinct union of both branches.

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
        receiver_ids = [row[0] for row in rows]
        try:
            v2_rows = (
                db.query(InboxModel.receiver_id)
                .join(
                    ManagedLaunchV2TerminalModel,
                    ManagedLaunchV2TerminalModel.id == InboxModel.receiver_id,
                )
                .filter(
                    InboxModel.status == MessageStatus.PENDING.value,
                    InboxModel.created_at < cutoff,
                    *_live_managed_v2_terminal_clauses(),
                )
                .distinct()
                .all()
            )
        except OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            v2_rows = []
        seen = set(receiver_ids)
        receiver_ids.extend(row[0] for row in v2_rows if row[0] not in seen)
        return receiver_ids


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal metadata."""
    with SessionLocal() as db:
        deleted = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).delete()
        db.commit()
        return deleted > 0


def delete_terminal_if_generation(terminal_id: str, generation: str) -> bool:
    """Atomically delete terminal metadata only for the exact incarnation."""
    with SessionLocal() as db:
        deleted = (
            db.query(TerminalModel)
            .filter(
                TerminalModel.id == terminal_id,
                TerminalModel.generation == generation,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted == 1


def delete_terminals_by_session(tmux_session: str) -> int:
    """Delete all terminals in a session."""
    with SessionLocal() as db:
        deleted = (
            db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).delete()
        )
        db.commit()
        return deleted


def create_inbox_message(sender_id: str, receiver_id: str, message: str) -> InboxMessage:
    """Create inbox message with status=MessageStatus.PENDING.

    The receiver is validated cross-vintage: a legacy ``terminals`` row, or
    exactly one current, live, non-superseded managed-v2 identity
    (``_inbox_receiver_eligible``). The inbox row itself stays
    vintage-agnostic (``receiver_id`` string only).

    Raises:
        ValueError: If the receiver terminal does not exist, is not a live
            managed-v2 identity, or is ambiguous across protocol vintages.
    """
    with SessionLocal() as db:
        if not _inbox_receiver_eligible(db, receiver_id):
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


def is_message_pending(message_id: int) -> bool:
    """Return whether one exact inbox row is still eligible for delivery."""
    with SessionLocal() as db:
        return (
            db.query(InboxModel.id)
            .filter(
                InboxModel.id == message_id,
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .first()
            is not None
        )


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
    """Update one inbox row, conditionally terminalizing a pending delivery.

    The unmanaged pane path uses ``PENDING -> DELIVERED`` as its effect claim.
    The managed bridge path calls it only after provider acknowledgement, with
    the condition retained as defense in depth. Rollback/failure transitions
    remain unconditional because only the caller that owns the corresponding
    delivery attempt issues them.
    """
    with SessionLocal() as db:
        if status == MessageStatus.DELIVERED:
            updated = (
                db.query(InboxModel)
                .filter(
                    InboxModel.id == message_id,
                    InboxModel.status == MessageStatus.PENDING.value,
                )
                .update(
                    {InboxModel.status: MessageStatus.DELIVERED.value},
                    synchronize_session=False,
                )
            )
            db.commit()
            return updated == 1

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
