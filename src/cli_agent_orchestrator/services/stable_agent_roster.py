"""M3-A / cond-0377: the fork-owned stable CAO-agent roster.

CAO separates four identities so it can reclaim disposable panes without
erasing the coding agents' working memory::

    CAO session
      -> stable CAO agent (role/profile family)
        -> harness-native conversation lineage (append-only)
          -> disposable incarnation (terminal generation, pane, process)

This module owns the durable roster for the stable agent and its lineage/
incarnation history.  It exists independently of tmux — Stop/Resume must
work without a conductor — and it is deliberately *not* a second physical
roster: the terminal, reservation, and native-attachment stores remain the
authorities for the physical and provider-native facts, and this roster
binds them to one stable identity.

Record truth (per agent): session, role/profile family, disposition,
resume-contract version, current-lineage/current-incarnation pointers,
timestamps and a strictly increasing revision.  Per lineage: harness
identity, native session id or the truthful ``identity_missing`` state,
bounded route-provider provenance, acquisition method, bounded continuity
truth, and the predecessor link — a fresh fallback adds a lineage linked
to its predecessor and never overwrites the failed/missing one.

``agent_id`` is an explicit immutable identity minted from the durable
initial physical launch identity by the launch seam — never inferred from
role/profile.  A CAO session may hold many workers of one profile, each
with an independent native conversation and retirement/resume history;
session, role, and profile family are attributes that must match on
replay, not a uniqueness key.  Later reincarnations pass the same
``agent_id`` explicitly.

Deterministic create/adopt: a replayed or concurrent bind of the same
contract adopts the existing rows, and a conflicting immutable identity
(a changed session/role/profile for an existing agent id, a different
native id for the same lineage, the same (harness, native id) for two
agents, the same terminal for two lineages, a live id attached to two
incarnations) is refused with zero mutation.  One (harness, native
session id) pair maps to exactly one lineage, therefore to one stable
agent; two unrelated harnesses may legally emit the same textual id, so
uniqueness is scoped to the harness.  DeepSeek and Z.ai are Claude Code
route values under the ``claude_code`` harness and share its scope.  The
exclusive live ownership of the provider session itself remains enforced
by the native-attachment authority (``services/native_attachment.py``).

Real task input/admission is gated on the durable binding state: the
incarnation must exist, not be retired, and carry a bound native identity
before ``assert_admission_ready`` passes.  Legacy, missing, corrupt, or
unknown-version rows degrade truthfully: list/read/audit never crash on
them and unrelated launches are never blocked.

Storage: three additive ORM tables (``stable_agents``,
``stable_agent_lineages``, ``stable_agent_incarnations``) created by
``clients/database.py`` (``create_all`` for fresh databases,
``_migrate_stable_agent_roster`` for existing ones).  All mutation runs
inside one SQLite transaction; no file or database lock is ever held
across provider, tmux, or network I/O.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import native_attachment

#: Versioned identity contract of the roster itself.  A reader that sees an
#: unknown ``resume_contract_version`` must degrade truthfully (flag it in
#: the audit) rather than assume semantics it cannot prove.
RESUME_CONTRACT_VERSION = "cao-m3-resume-contract-v1"

ROLE_SUPERVISOR = "supervisor"
ROLE_WORKER = "worker"
ROLES = frozenset({ROLE_SUPERVISOR, ROLE_WORKER})

#: Fixed namespace for deterministic initial agent ids seeded by the
#: durable physical launch identity.  The derived id is the DURABLE
#: identity: later reincarnations pass the same agent_id explicitly, so
#: terminal identity only ever seeds the initial mint, never keys the
#: identity forever.
_AGENT_ID_NAMESPACE = uuid.UUID("03770000-0000-4000-8000-000000000000")

#: Agent dispositions.  ``identity_missing`` is a truthful state — the
#: current lineage has no native session id — never a blocker for Stop.
DISPOSITION_LIVE = "live"
DISPOSITION_DORMANT = "dormant"
DISPOSITION_IDENTITY_MISSING = "identity_missing"
DISPOSITION_RETIRED = "retired"
DISPOSITIONS = frozenset(
    {DISPOSITION_LIVE, DISPOSITION_DORMANT, DISPOSITION_IDENTITY_MISSING, DISPOSITION_RETIRED}
)

#: Incarnation dispositions.  ``admitted`` is the durable state that gates
#: real task input; ``retired`` preserves the row as history.
INCARNATION_BOUND = "bound"
INCARNATION_ADMITTED = "admitted"
INCARNATION_RETIRED = "retired"
INCARNATION_DISPOSITIONS = frozenset({INCARNATION_BOUND, INCARNATION_ADMITTED, INCARNATION_RETIRED})
LIVE_INCARNATION_DISPOSITIONS = frozenset({INCARNATION_BOUND, INCARNATION_ADMITTED})

LINEAGE_ORIGIN_INITIAL = "initial"
LINEAGE_ORIGIN_RESUME = "resume"
LINEAGE_ORIGIN_FALLBACK = "fallback"
LINEAGE_ORIGIN_ADOPT = "adopt"
LINEAGE_ORIGIN_REPAIR = "repair"
LINEAGE_ORIGINS = frozenset(
    {
        LINEAGE_ORIGIN_INITIAL,
        LINEAGE_ORIGIN_RESUME,
        LINEAGE_ORIGIN_FALLBACK,
        LINEAGE_ORIGIN_ADOPT,
        LINEAGE_ORIGIN_REPAIR,
    }
)

#: Closed vocabulary of bounded route-provider provenance.  DeepSeek and
#: Z.ai are Claude Code routes — the ``provider_route`` value travels
#: beside the id and never changes its harness domain.
ROUTE_PROVENANCE_KEYS = frozenset(
    {"provider_route", "assigned_policy_sha256", "route_payload_sha256", "issuance_source"}
)
ROUTE_PROVENANCE_VALUE_MAX = 512
CONTINUITY_NOTE_MAX = 200
NATIVE_SESSION_ID_MAX = 512

#: Acquisition-method vocabulary is owned by the native-attachment
#: authority; this roster records the same closed set so the two never
#: disagree about how an id came to exist.
ACQUISITION_CHOSEN_SESSION_ID = native_attachment.ACQUISITION_CHOSEN_SESSION_ID
ACQUISITION_RESUME = native_attachment.ACQUISITION_RESUME
ACQUISITION_ACP_BOOTSTRAP = native_attachment.ACQUISITION_ACP_BOOTSTRAP
ACQUISITION_ZERO_TURN_BOOTSTRAP = native_attachment.ACQUISITION_ZERO_TURN_BOOTSTRAP
ACQUISITION_METHODS = native_attachment.ACQUISITION_METHODS


class StableAgentError(RuntimeError):
    """Base error for roster operations."""

    code = "stable-agent-error"


class StableAgentInvalid(StableAgentError):
    """A supplied contract value is malformed or an obligation is unmet."""

    code = "stable-agent-invalid"


class StableAgentConflict(StableAgentError):
    """The bind conflicts with an immutable identity already on record."""

    code = "stable-agent-conflict"


class StableAgentNotFound(StableAgentError):
    """No stable-agent record exists for the requested identity."""

    code = "stable-agent-not-found"


class StableAgentAdmissionRefused(StableAgentError):
    """Task input/admission is refused because the binding is not durable."""

    code = "stable-agent-admission-refused"


class StableAgentUnavailable(StableAgentError):
    """The roster store could not be read or written."""

    code = "stable-agent-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def roster_store_present(db: Any = None) -> bool:
    """Whether the roster tables exist on this store (read/capability).

    Dark and additive: a store that has never run the M3-A migration has
    no roster.  This is a READ surface for operators/audit — it is NOT a
    launch gate: the managed bind/admission seams fail closed after this
    build's initialization rather than bypassing on absence.
    """
    from sqlalchemy import inspect as sa_inspect

    def _check(session: Any) -> bool:
        inspector = sa_inspect(session.get_bind())
        result = inspector.has_table(database.StableAgentModel.__tablename__)
        return bool(result)

    if db is not None:
        return _check(db)
    with database.SessionLocal() as session:
        return _check(session)


def derive_initial_agent_id(terminal_id: str, generation: Optional[str] = None) -> str:
    """Deterministic initial stable-agent id for one physical launch.

    Seeded by the immutable initial physical identity — the terminal id
    plus, when present, the initial generation — both durably allocated
    before any provider effect, so a response-lost replay of the same
    physical launch derives the same id and an unrelated initial launch
    that reuses a terminal id with a new generation can never inherit the
    prior stable agent.  Unmanaged launches carry no generation and seed
    from the terminal id alone (their only durable physical identity).

    The result is the durable identity: role/profile are attributes of it,
    never its key, and later reincarnations pass it explicitly.
    """
    terminal_id = _require_text(terminal_id, field="terminal_id", max_len=64)
    if generation is not None:
        generation = _require_text(generation, field="generation", max_len=128)
        seed = f"cao-stable-agent-v1:{terminal_id}:{generation}"
    else:
        seed = f"cao-stable-agent-v1:{terminal_id}"
    return str(uuid.uuid5(_AGENT_ID_NAMESPACE, seed))


def _require_text(value: Any, *, field: str, max_len: int = 512) -> str:
    if not isinstance(value, str) or not value:
        raise StableAgentInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise StableAgentInvalid(f"{field} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field: str, max_len: int) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field=field, max_len=max_len)


def _validate_route_provenance(value: Any) -> Optional[dict[str, Any]]:
    """Bound and closed route provenance: only the known keys, bounded values."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StableAgentInvalid(f"route_provenance must be a mapping; got {value!r}")
    unknown = sorted(set(value) - ROUTE_PROVENANCE_KEYS)
    if unknown:
        raise StableAgentInvalid(
            f"route_provenance carries unknown key(s) {unknown}; the vocabulary is closed to "
            f"{sorted(ROUTE_PROVENANCE_KEYS)}"
        )
    for key, item in value.items():
        if not isinstance(item, str) or not item:
            raise StableAgentInvalid(f"route_provenance.{key} must be a non-empty string")
        if len(item) > ROUTE_PROVENANCE_VALUE_MAX:
            raise StableAgentInvalid(
                f"route_provenance.{key} must be at most {ROUTE_PROVENANCE_VALUE_MAX} characters"
            )
        if key.endswith("_sha256") and (
            len(item) != 64 or any(ch not in "0123456789abcdef" for ch in item)
        ):
            raise StableAgentInvalid(f"route_provenance.{key} must be 64 lowercase hex characters")
    return dict(value)


def _validate_process_identity(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StableAgentInvalid(f"process_identity must be a mapping; got {value!r}")
    pid = value.get("pid")
    start_marker = value.get("start_marker")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise StableAgentInvalid(f"process_identity.pid must be a positive integer; got {pid!r}")
    if not isinstance(start_marker, str) or not start_marker:
        raise StableAgentInvalid(
            f"process_identity.start_marker must be a non-empty string; got {start_marker!r}"
        )
    return native_attachment.process_identity(pid=pid, start_marker=start_marker)


@dataclass(frozen=True)
class BindingContract:
    """The immutable launch/binding facts one stable-agent bind is made of.

    ``agent_id`` is the explicit durable identity — minted from the
    initial physical launch identity by the launch seam, and reused
    explicitly by later reincarnations.  Session, role, and profile family
    are attributes that must match on replay; they are never a key.

    The contract is what makes create/adopt deterministic: the same
    contract replays to the same rows, and a changed immutable identity
    (a different agent_id for one lineage, or changed session/role/profile
    facts for an existing agent_id) is refused rather than silently
    rewriting history.
    """

    agent_id: str
    session_name: str
    role: str
    profile_family: str
    harness: str
    native_session_id: Optional[str] = None
    acquisition_method: Optional[str] = None
    route_provenance: Optional[dict[str, Any]] = None
    continuity_note: Optional[str] = None
    terminal_id: Optional[str] = None
    generation: Optional[str] = None
    pane_id: Optional[str] = None
    pane_pid: Optional[int] = None
    process_identity: Optional[dict[str, Any]] = None
    execution_mode: Optional[str] = None
    #: Caller truthfulness for lineage creation: initial/resume/fallback/
    #: adopt.  ``None`` derives from the agent's history (fallback when a
    #: predecessor lineage exists, initial otherwise).
    lineage_origin: Optional[str] = None
    #: The incarnation is recorded already-admitted (legacy adoption of a
    #: live terminal); never used to skip the durable binding of a new one.
    admitted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id:
            raise StableAgentInvalid(f"agent_id must be a non-empty string; got {self.agent_id!r}")
        try:
            if str(uuid.UUID(self.agent_id)) != self.agent_id:
                raise ValueError
        except ValueError as exc:
            raise StableAgentInvalid(
                f"agent_id must be a canonical lowercase UUID; got {self.agent_id!r}"
            ) from exc
        _require_text(self.session_name, field="session_name")
        if self.role not in ROLES:
            raise StableAgentInvalid(f"role must be one of {sorted(ROLES)}; got {self.role!r}")
        _require_text(self.profile_family, field="profile_family")
        _require_text(self.harness, field="harness")
        object.__setattr__(
            self,
            "native_session_id",
            _optional_text(
                self.native_session_id, field="native_session_id", max_len=NATIVE_SESSION_ID_MAX
            ),
        )
        if self.acquisition_method is not None:
            if self.acquisition_method not in ACQUISITION_METHODS:
                raise StableAgentInvalid(
                    f"acquisition_method must be one of {sorted(ACQUISITION_METHODS)}; "
                    f"got {self.acquisition_method!r}"
                )
            if self.native_session_id is None:
                raise StableAgentInvalid(
                    "acquisition_method records how a native id was obtained; it is "
                    "meaningless without one"
                )
        object.__setattr__(
            self, "route_provenance", _validate_route_provenance(self.route_provenance)
        )
        object.__setattr__(
            self,
            "continuity_note",
            _optional_text(
                self.continuity_note, field="continuity_note", max_len=CONTINUITY_NOTE_MAX
            ),
        )
        if self.terminal_id is not None:
            object.__setattr__(
                self,
                "terminal_id",
                _require_text(self.terminal_id, field="terminal_id", max_len=64),
            )
        if self.generation is not None:
            object.__setattr__(
                self,
                "generation",
                _require_text(self.generation, field="generation", max_len=128),
            )
        if self.pane_id is not None:
            object.__setattr__(
                self, "pane_id", _require_text(self.pane_id, field="pane_id", max_len=128)
            )
        if self.pane_pid is not None and (
            not isinstance(self.pane_pid, int)
            or isinstance(self.pane_pid, bool)
            or self.pane_pid <= 0
        ):
            raise StableAgentInvalid(f"pane_pid must be a positive integer; got {self.pane_pid!r}")
        object.__setattr__(
            self, "process_identity", _validate_process_identity(self.process_identity)
        )
        if self.execution_mode is not None:
            object.__setattr__(
                self,
                "execution_mode",
                _require_text(self.execution_mode, field="execution_mode", max_len=64),
            )
        if self.lineage_origin is not None and self.lineage_origin not in LINEAGE_ORIGINS:
            raise StableAgentInvalid(
                f"lineage_origin must be one of {sorted(LINEAGE_ORIGINS)}; "
                f"got {self.lineage_origin!r}"
            )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _agent_dict(row: Any) -> dict[str, Any]:
    known = row.disposition in DISPOSITIONS
    return {
        "agent_id": row.agent_id,
        "session_name": row.session_name,
        "role": row.role,
        "profile_family": row.profile_family,
        "disposition": row.disposition,
        "disposition_known": known,
        "resume_contract_version": row.resume_contract_version,
        "current_lineage_id": row.current_lineage_id,
        "current_incarnation_id": row.current_incarnation_id,
        "revision": row.revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _lineage_dict(row: Any) -> dict[str, Any]:
    return {
        "lineage_id": row.lineage_id,
        "agent_id": row.agent_id,
        "harness": row.harness,
        "native_session_id": row.native_session_id,
        "acquisition_method": row.acquisition_method,
        "route_provenance": _parse_json(row.route_provenance_json),
        "continuity_note": row.continuity_note,
        "predecessor_lineage_id": row.predecessor_lineage_id,
        "lineage_origin": row.lineage_origin,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _incarnation_dict(row: Any) -> dict[str, Any]:
    return {
        "incarnation_id": row.incarnation_id,
        "agent_id": row.agent_id,
        "lineage_id": row.lineage_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "pane_id": row.pane_id,
        "pane_pid": row.pane_pid,
        "process_identity": _parse_json(row.process_identity_json),
        "execution_mode": row.execution_mode,
        "disposition": row.disposition,
        "retired_at": row.retired_at,
        "retirement_reason": row.retirement_reason,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _agent_by_id(db: Any, agent_id: str) -> Any:
    return (
        db.query(database.StableAgentModel)
        .filter(database.StableAgentModel.agent_id == agent_id)
        .one_or_none()
    )


def _lineage_by_harness_native_id(db: Any, harness: str, native_session_id: str) -> Any:
    """The one lineage for a (harness, native_session_id) pair.

    Uniqueness is scoped to the harness: two unrelated harnesses may
    legally emit the same textual id, so a Claude and a Muse lineage with
    the same raw string are independent.
    """
    return (
        db.query(database.StableAgentLineageModel)
        .filter(
            database.StableAgentLineageModel.harness == harness,
            database.StableAgentLineageModel.native_session_id == native_session_id,
        )
        .one_or_none()
    )


def _lineage_by_id(db: Any, lineage_id: str) -> Any:
    return (
        db.query(database.StableAgentLineageModel)
        .filter(database.StableAgentLineageModel.lineage_id == lineage_id)
        .one_or_none()
    )


def _incarnation_by_exact(db: Any, terminal_id: str, generation: Optional[str] = None) -> Any:
    """The incarnation for exactly one (terminal_id, generation) pair.

    Physical identity everywhere else is terminal + generation: a later
    generation may reuse a terminal id, and history must stay readable
    rather than collide.  A row whose generation is NULL (legacy unmanaged
    launch) is matched only by a NULL-generation lookup.
    """
    query = db.query(database.StableAgentIncarnationModel).filter(
        database.StableAgentIncarnationModel.terminal_id == terminal_id
    )
    if generation is None:
        query = query.filter(database.StableAgentIncarnationModel.generation.is_(None))
    else:
        query = query.filter(database.StableAgentIncarnationModel.generation == generation)
    return query.one_or_none()


def _live_incarnation_by_terminal(db: Any, terminal_id: str) -> Any:
    """The unique live (bound|admitted) incarnation of a terminal id.

    A terminal-only read resolves the unique current/live incarnation
    deterministically; two live incarnations sharing a terminal id
    (corrupt/legacy state) refuse rather than pick an arbitrary row.
    """
    rows = (
        db.query(database.StableAgentIncarnationModel)
        .filter(
            database.StableAgentIncarnationModel.terminal_id == terminal_id,
            database.StableAgentIncarnationModel.disposition.in_(
                sorted(LIVE_INCARNATION_DISPOSITIONS)
            ),
        )
        .order_by(
            database.StableAgentIncarnationModel.created_at,
            database.StableAgentIncarnationModel.incarnation_id,
        )
        .all()
    )
    if len(rows) > 1:
        raise StableAgentConflict(
            f"terminal-only lookup for {terminal_id} is ambiguous: "
            f"{len(rows)} live incarnations share the terminal id; "
            "resolve by exact generation instead of picking a historical row"
        )
    return rows[0] if rows else None


def _agent_has_live_incarnation(
    db: Any, agent_id: str, *, except_incarnation_id: Optional[str] = None
) -> bool:
    """Whether the stable agent already holds a live incarnation.

    At most one live ``bound|admitted`` incarnation may exist per stable
    agent regardless of lineage: a fresh fallback or resume must first
    retire/fence the predecessor, or the agent would be duplicated.
    """
    query = db.query(database.StableAgentIncarnationModel).filter(
        database.StableAgentIncarnationModel.agent_id == agent_id,
        database.StableAgentIncarnationModel.disposition.in_(sorted(LIVE_INCARNATION_DISPOSITIONS)),
    )
    if except_incarnation_id is not None:
        query = query.filter(
            database.StableAgentIncarnationModel.incarnation_id != except_incarnation_id
        )
    return query.first() is not None


def _check_lineage_can_attach(
    db: Any,
    lineage: Any,
    *,
    agent: Any,
    harness: str,
) -> None:
    """Refuse cross-agent and cross-harness lineage attachment.

    The live-incarnation exclusion is enforced per STABLE AGENT (at most
    one live incarnation regardless of lineage) by the bind/repair paths,
    which run before any lineage resolution — never per lineage.
    """
    if lineage.agent_id != agent.agent_id:
        raise StableAgentConflict(
            f"native session {lineage.native_session_id!r} is already attached to stable "
            f"agent {lineage.agent_id!r}; one native id can never be live-attached to two "
            "stable agents"
        )
    if lineage.harness != harness:
        raise StableAgentConflict(
            f"native session {lineage.native_session_id!r} belongs to harness "
            f"{lineage.harness!r}; native ids never cross harness domains and a "
            f"{harness!r} bind is refused"
        )


def _bind_incarnation_to_lineage(
    db: Any,
    incarnation: Any,
    lineage: Any,
    *,
    contract: BindingContract,
    agent: Any,
) -> None:
    """Link a pending incarnation to a resolved lineage (repair/resume)."""
    _check_lineage_can_attach(db, lineage, agent=agent, harness=contract.harness)
    incarnation.lineage_id = lineage.lineage_id
    incarnation.execution_mode = (
        contract.execution_mode
        if contract.execution_mode is not None
        else incarnation.execution_mode
    )
    incarnation.updated_at = _now()
    agent.current_lineage_id = lineage.lineage_id
    agent.current_incarnation_id = incarnation.incarnation_id
    agent.disposition = _agent_disposition(db, agent, lineage)
    agent.revision = int(agent.revision or 0) + 1
    agent.updated_at = _now()


def _create_lineage(
    db: Any, agent: Any, contract: BindingContract, *, origin: str, predecessor: Optional[str]
) -> Any:
    stamp = _now()
    lineage = database.StableAgentLineageModel(
        lineage_id=str(uuid.uuid4()),
        agent_id=agent.agent_id,
        harness=contract.harness,
        native_session_id=contract.native_session_id,
        acquisition_method=contract.acquisition_method,
        route_provenance_json=(
            _canonical_json(contract.route_provenance) if contract.route_provenance else None
        ),
        continuity_note=contract.continuity_note,
        predecessor_lineage_id=predecessor,
        lineage_origin=origin,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(lineage)
    return lineage


def _resolve_lineage(
    db: Any,
    agent: Any,
    contract: BindingContract,
    *,
    incarnation: Optional[Any],
    pending_lineage: Optional[Any],
) -> tuple[Any, bool]:
    """Resolve the lineage a bind attaches to; returns (lineage, created).

    ``pending_lineage`` is the incarnation's own lineage when the
    incarnation already exists (identity pending or missing).
    """
    native_id = contract.native_session_id
    if native_id is not None:
        existing = _lineage_by_harness_native_id(db, contract.harness, native_id)
        if existing is not None:
            _check_lineage_can_attach(db, existing, agent=agent, harness=contract.harness)
            return existing, False
        predecessor = agent.current_lineage_id
        origin = contract.lineage_origin or (
            LINEAGE_ORIGIN_FALLBACK if predecessor is not None else LINEAGE_ORIGIN_INITIAL
        )
        lineage = _create_lineage(db, agent, contract, origin=origin, predecessor=predecessor)
        return lineage, True

    # No native id in the contract: the lineage stays truthful identity_missing.
    if pending_lineage is not None and pending_lineage.native_session_id is None:
        return pending_lineage, False
    if pending_lineage is not None and pending_lineage.native_session_id is not None:
        raise StableAgentConflict(
            f"binding without a native id is refused for an incarnation whose lineage "
            f"{pending_lineage.lineage_id} is already bound to "
            f"{pending_lineage.native_session_id!r}; a known binding is never overwritten "
            "by a missing identity"
        )
    predecessor = agent.current_lineage_id
    origin = contract.lineage_origin or (
        LINEAGE_ORIGIN_FALLBACK if predecessor is not None else LINEAGE_ORIGIN_INITIAL
    )
    lineage = _create_lineage(db, agent, contract, origin=origin, predecessor=predecessor)
    return lineage, True


def _agent_disposition(db: Any, agent: Any, current_lineage: Optional[Any]) -> str:
    if current_lineage is None or current_lineage.native_session_id is None:
        return DISPOSITION_IDENTITY_MISSING
    return DISPOSITION_LIVE


def _bind_once(db: Any, contract: BindingContract) -> dict[str, Any]:
    """One create/adopt pass inside the caller's transaction/savepoint.

    The agent is resolved by the explicit ``agent_id``.  Session, role,
    and profile family are immutable attributes of the id: an existing
    agent whose attributes differ from the contract is a conflicting
    identity and refuses with zero mutation.  A different physical
    initial launch (different agent_id) with the same session/role/
    profile is a different agent.
    """
    agent = _agent_by_id(db, contract.agent_id)
    created_agent = False
    if agent is None:
        stamp = _now()
        agent = database.StableAgentModel(
            agent_id=contract.agent_id,
            session_name=contract.session_name,
            role=contract.role,
            profile_family=contract.profile_family,
            disposition=DISPOSITION_IDENTITY_MISSING,
            resume_contract_version=RESUME_CONTRACT_VERSION,
            revision=1,
            created_at=stamp,
            updated_at=stamp,
        )
        db.add(agent)
        db.flush()
        created_agent = True
    else:
        mismatches = {
            "session_name": (agent.session_name, contract.session_name),
            "role": (agent.role, contract.role),
            "profile_family": (agent.profile_family, contract.profile_family),
        }
        differing = {
            key: {"recorded": recorded, "contract": incoming}
            for key, (recorded, incoming) in mismatches.items()
            if recorded != incoming
        }
        if differing:
            raise StableAgentConflict(
                f"stable agent {contract.agent_id} is already recorded with immutable "
                f"attributes that differ from this contract: {differing}"
            )

    incarnation = (
        _incarnation_by_exact(db, contract.terminal_id, contract.generation)
        if contract.terminal_id is not None
        else None
    )
    pending_lineage: Optional[Any] = None
    if incarnation is not None:
        if incarnation.agent_id != agent.agent_id:
            raise StableAgentConflict(
                f"terminal {contract.terminal_id} generation {contract.generation} is already "
                f"recorded under stable agent {incarnation.agent_id!r}; one physical "
                "incarnation can never belong to two stable agents"
            )
        if incarnation.lineage_id is not None:
            pending_lineage = _lineage_by_id(db, incarnation.lineage_id)
            if pending_lineage is None:
                raise StableAgentUnavailable(
                    f"incarnation {incarnation.incarnation_id} names a missing lineage"
                )
            if pending_lineage.agent_id != agent.agent_id:
                raise StableAgentConflict(
                    f"incarnation lineage {pending_lineage.lineage_id} belongs to a different "
                    "stable agent; the binding is refused"
                )

    # At most one live incarnation per stable agent, regardless of
    # lineage.  This runs BEFORE any lineage/incarnation creation so a
    # refusal leaves zero new rows: a fresh fallback or resume must first
    # retire/fence the predecessor incarnation.  An exact replay of this
    # incarnation is excluded and stays idempotent.
    if incarnation is None and _agent_has_live_incarnation(
        db, agent.agent_id, except_incarnation_id=None
    ):
        raise StableAgentConflict(
            f"stable agent {agent.agent_id} already has a live incarnation; a new "
            "incarnation must first retire/fence the predecessor (one live incarnation "
            "per stable agent regardless of lineage)"
        )

    lineage, lineage_created = _resolve_lineage(
        db, agent, contract, incarnation=incarnation, pending_lineage=pending_lineage
    )

    incarnation_created = False
    if incarnation is None:
        stamp = _now()
        incarnation = database.StableAgentIncarnationModel(
            incarnation_id=str(uuid.uuid4()),
            agent_id=agent.agent_id,
            # An identity-less bind still links the incarnation to its
            # lineage (the truthful ``identity_missing`` record) so a later
            # repair can bind the discovered id onto that exact lineage.
            lineage_id=lineage.lineage_id,
            terminal_id=contract.terminal_id,
            generation=contract.generation,
            pane_id=contract.pane_id,
            pane_pid=contract.pane_pid,
            process_identity_json=(
                _canonical_json(contract.process_identity) if contract.process_identity else None
            ),
            execution_mode=contract.execution_mode,
            disposition=INCARNATION_ADMITTED if contract.admitted else INCARNATION_BOUND,
            created_at=stamp,
            updated_at=stamp,
        )
        db.add(incarnation)
        db.flush()
        incarnation_created = True
    elif contract.native_session_id is not None:
        if incarnation.lineage_id is None or pending_lineage is None:
            # Identity pending: bind the resolved lineage (repair/resume).
            _bind_incarnation_to_lineage(db, incarnation, lineage, contract=contract, agent=agent)
        elif pending_lineage.lineage_id == lineage.lineage_id:
            # Exact replay of the same bind: adopt, never regress.
            pass
        elif pending_lineage.native_session_id is None:
            # The pending missing lineage is superseded by the real id's
            # lineage; this incarnation joins the real one.
            _bind_incarnation_to_lineage(db, incarnation, lineage, contract=contract, agent=agent)
        else:
            raise StableAgentConflict(
                f"terminal {contract.terminal_id} is bound to lineage "
                f"{incarnation.lineage_id!r}; lineage {lineage.lineage_id!r} is refused — "
                "an incarnation's native identity is immutable once bound"
            )

    if incarnation_created and (
        agent.current_lineage_id != lineage.lineage_id
        or agent.current_incarnation_id != incarnation.incarnation_id
    ):
        agent.current_lineage_id = lineage.lineage_id
        agent.current_incarnation_id = incarnation.incarnation_id
        agent.disposition = _agent_disposition(db, agent, lineage)
        agent.revision = int(agent.revision or 0) + 1
        agent.updated_at = _now()

    db.flush()
    return {
        "agent": _agent_dict(agent),
        "lineage": _lineage_dict(lineage),
        "incarnation": _incarnation_dict(incarnation),
        "adopted": not (created_agent or lineage_created or incarnation_created),
    }


def bind_generation(contract: BindingContract, db: Any = None) -> dict[str, Any]:
    """Create or adopt the stable agent, lineage, and incarnation for one
    launch contract; returns the resulting records.

    Deterministic create/adopt: the same contract replays to the same
    rows.  A conflicting immutable identity refuses with zero mutation.

    ``db`` — when supplied (the managed-launch seam passes its own open
    session so the roster write commits atomically with the reservation
    ``bound`` transition), the write runs inside a savepoint and the
    caller owns the commit.  When omitted, a standalone transaction is
    opened and committed, and a concurrent duplicate is adopted on retry.
    """
    if not isinstance(contract, BindingContract):
        raise StableAgentInvalid(f"contract must be a BindingContract; got {contract!r}")
    if db is not None:
        try:
            with db.begin_nested():
                return _bind_once(db, contract)
        except IntegrityError as exc:
            # A concurrent writer won a unique slot.  The savepoint is
            # rolled back; the managed-launch protocol retries the bind and
            # adopts the winner's rows on that retry.
            raise StableAgentUnavailable(
                f"concurrent roster write refused; retry the bind to adopt: {exc}"
            ) from exc
    import time

    from sqlalchemy.exc import OperationalError

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                with session.begin_nested():
                    result = _bind_once(session, contract)
                session.commit()
                return result
        except IntegrityError as exc:
            # A concurrent writer won a unique slot between our read and
            # write.  Roll back and adopt the winner's rows on the retry.
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            # SQLite read->write upgrade contention: two writers cannot both
            # hold a read snapshot and upgrade.  Roll back (releasing the
            # snapshot) and retry; the other writer converges or commits.
            last_error = exc
            time.sleep(0.05)
    raise StableAgentUnavailable(
        f"concurrent roster writes kept conflicting; refusing after retry: {last_error}"
    )


def _find_incarnation(db: Any, *, terminal_id: str, generation: Optional[str] = None) -> Any:
    """Resolve the exact incarnation for effect safety.

    With an exact generation: that generation's incarnation, or a typed
    refusal when absent.  Without one (legacy teardown/repair callers):
    the unique live incarnation of the terminal, or a typed refusal —
    never an arbitrary historical row.
    """
    if generation is not None:
        incarnation = _incarnation_by_exact(db, terminal_id, generation)
        if incarnation is None:
            raise StableAgentAdmissionRefused(
                f"no stable-agent incarnation is recorded for terminal {terminal_id} "
                f"generation {generation}; real task input is impossible before the "
                "stable-agent/native binding reaches its durable state"
            )
        return incarnation
    incarnation = _live_incarnation_by_terminal(db, terminal_id)
    if incarnation is None:
        raise StableAgentAdmissionRefused(
            f"no live stable-agent incarnation is recorded for terminal {terminal_id}; "
            "real task input is impossible before the stable-agent/native binding "
            "reaches its durable state"
        )
    return incarnation


def assert_admission_ready(
    *, terminal_id: str, generation: Optional[str] = None, db: Any = None
) -> None:
    """The durable gate every real task-input lane must pass before submission.

    Resolves the exact generation when one is given (the managed launch
    seam always does), else the unique live incarnation.  Refuses when
    there is no roster incarnation, when the incarnation is retired, or
    when the lineage still lacks a native id (``identity_missing``) — a
    missing identity must be repaired or replaced by a fresh fallback,
    never admitted as if it were bound.
    """

    def _check(session: Any) -> None:
        incarnation = _find_incarnation(session, terminal_id=terminal_id, generation=generation)
        if incarnation.disposition == INCARNATION_RETIRED:
            raise StableAgentAdmissionRefused(
                f"incarnation {incarnation.incarnation_id} of terminal {terminal_id} is "
                "retired; real task input is refused"
            )
        if incarnation.lineage_id is None:
            raise StableAgentAdmissionRefused(
                f"incarnation {incarnation.incarnation_id} of terminal {terminal_id} has no "
                "bound native lineage yet; input is refused until the stable-agent/native "
                "binding is durable"
            )
        lineage = _lineage_by_id(session, incarnation.lineage_id)
        if lineage is None or lineage.native_session_id is None:
            raise StableAgentAdmissionRefused(
                f"incarnation {incarnation.incarnation_id} of terminal {terminal_id} is "
                f"{DISPOSITION_IDENTITY_MISSING} (no native session id); input is refused "
                "until the identity is repaired or a fresh fallback binds a new one"
            )

    if db is not None:
        _check(db)
        return
    with database.SessionLocal() as session:
        _check(session)


def mark_admitted(
    *, terminal_id: str, generation: Optional[str] = None, db: Any = None
) -> dict[str, Any]:
    """Record the durable admitted state of an incarnation (idempotent).

    Resolves the exact generation when one is given, else the unique live
    incarnation."""

    def _mark(session: Any) -> dict[str, Any]:
        incarnation = _find_incarnation(session, terminal_id=terminal_id, generation=generation)
        if incarnation.disposition == INCARNATION_RETIRED:
            raise StableAgentConflict(
                f"incarnation {incarnation.incarnation_id} is retired; a retired physical "
                "incarnation cannot become admitted"
            )
        if incarnation.disposition == INCARNATION_ADMITTED:
            return _incarnation_dict(incarnation)
        incarnation.disposition = INCARNATION_ADMITTED
        incarnation.updated_at = _now()
        session.flush()
        return _incarnation_dict(incarnation)

    if db is not None:
        with db.begin_nested():
            return _mark(db)
    with database.SessionLocal() as session:
        result = _mark(session)
        session.commit()
        return result


def retire_incarnation(
    *, terminal_id: str, generation: Optional[str] = None, reason: str, db: Any = None
) -> dict[str, Any]:
    """Retire a disposable incarnation; the stable agent and its history
    survive.  Resolves the exact generation when one is given (teardown
    passes the generation claim), else the unique live incarnation.
    Best-effort by contract: callers on teardown paths never let this
    raise out (Stop must not be blocked by roster bookkeeping)."""
    reason = _require_text(reason, field="reason", max_len=512)

    def _retire(session: Any) -> dict[str, Any]:
        incarnation = _find_incarnation(session, terminal_id=terminal_id, generation=generation)
        stamp = _now()
        if incarnation.disposition != INCARNATION_RETIRED:
            incarnation.disposition = INCARNATION_RETIRED
            incarnation.retired_at = stamp
            incarnation.retirement_reason = reason
            incarnation.updated_at = stamp
        agent = (
            session.query(database.StableAgentModel)
            .filter(database.StableAgentModel.agent_id == incarnation.agent_id)
            .one_or_none()
        )
        if agent is not None and agent.current_incarnation_id == incarnation.incarnation_id:
            agent.disposition = DISPOSITION_DORMANT
            agent.revision = int(agent.revision or 0) + 1
            agent.updated_at = stamp
        session.flush()
        return _incarnation_dict(incarnation)

    if db is not None:
        with db.begin_nested():
            return _retire(db)
    with database.SessionLocal() as session:
        result = _retire(session)
        session.commit()
        return result


def record_native_identity(
    *,
    terminal_id: str,
    native_session_id: str,
    harness: str,
    generation: Optional[str] = None,
    route_provenance: Optional[dict[str, Any]] = None,
    acquisition_method: Optional[str] = None,
    continuity_note: Optional[str] = None,
    db: Any = None,
) -> dict[str, Any]:
    """The repair seam: bind a discovered native identity onto an
    incarnation whose lineage is still ``identity_missing``.

    Resolves the exact generation when one is given, else the unique live
    incarnation of the terminal — never an arbitrary historical row.
    Refuses a conflicting id (the lineage is already bound to a different
    one), any cross-agent or cross-harness attachment, and any repair that
    would leave a second live incarnation on the stable agent.  A known
    binding is never overwritten.

    Concurrency: a lost unique-index race (another repairer binding the
    same ``(harness, native_session_id)`` first) is converted into
    deterministic exact-adopt or a typed conflict/unavailable with bounded
    retry — a raw SQLAlchemy ``IntegrityError`` never escapes.
    """
    native_session_id = _require_text(
        native_session_id, field="native_session_id", max_len=NATIVE_SESSION_ID_MAX
    )
    harness = _require_text(harness, field="harness")
    route_provenance = _validate_route_provenance(route_provenance)
    if acquisition_method is not None and acquisition_method not in ACQUISITION_METHODS:
        raise StableAgentInvalid(f"acquisition_method must be one of {sorted(ACQUISITION_METHODS)}")
    continuity_note = _optional_text(
        continuity_note, field="continuity_note", max_len=CONTINUITY_NOTE_MAX
    )

    def _repair(session: Any) -> dict[str, Any]:
        incarnation = _find_incarnation(session, terminal_id=terminal_id, generation=generation)
        agent = (
            session.query(database.StableAgentModel)
            .filter(database.StableAgentModel.agent_id == incarnation.agent_id)
            .one_or_none()
        )
        if agent is None:  # pragma: no cover - FK-less store, defensive
            raise StableAgentUnavailable(
                f"incarnation {incarnation.incarnation_id} has no stable agent row"
            )
        # A retired incarnation is a dead physical terminal: repairing its
        # identity would revive a retired record and repoint the agent at a
        # live disposition that no live incarnation backs.  Refused in the
        # same transaction as the retirement (i-0025).
        if incarnation.disposition == INCARNATION_RETIRED:
            raise StableAgentConflict(
                f"incarnation {incarnation.incarnation_id} of terminal {terminal_id} "
                "is retired; native-identity repair is refused for a retired "
                "incarnation — reincarnate on a fresh incarnation instead"
            )
        # One live incarnation per stable agent: a repair that would link a
        # live incarnation while another live incarnation already exists is
        # refused (legacy/corrupt state defense; bind already enforces this).
        if (
            incarnation.disposition in LIVE_INCARNATION_DISPOSITIONS
            and _agent_has_live_incarnation(
                session, agent.agent_id, except_incarnation_id=incarnation.incarnation_id
            )
        ):
            raise StableAgentConflict(
                f"stable agent {agent.agent_id} already has another live incarnation; "
                "the repair would leave two live incarnations and is refused"
            )
        existing = _lineage_by_harness_native_id(session, harness, native_session_id)
        if existing is not None and existing.lineage_id != incarnation.lineage_id:
            _check_lineage_can_attach(session, existing, agent=agent, harness=harness)
            if incarnation.lineage_id is not None:
                pending = _lineage_by_id(session, incarnation.lineage_id)
                if pending is not None and pending.native_session_id is not None:
                    raise StableAgentConflict(
                        f"terminal {terminal_id} is already bound to lineage "
                        f"{incarnation.lineage_id!r} (native session {pending.native_session_id!r}); "
                        f"binding {native_session_id!r} would overwrite a known identity"
                    )
            incarnation.lineage_id = existing.lineage_id
            lineage = existing
        elif incarnation.lineage_id is not None:
            lineage = _lineage_by_id(session, incarnation.lineage_id)
            if lineage is None:
                raise StableAgentUnavailable(
                    f"incarnation {incarnation.incarnation_id} names a missing lineage"
                )
            if lineage.agent_id != agent.agent_id:
                raise StableAgentConflict(
                    f"lineage {lineage.lineage_id} belongs to a different stable agent"
                )
            if lineage.harness != harness:
                raise StableAgentConflict(
                    f"lineage {lineage.lineage_id} belongs to harness {lineage.harness!r}; "
                    f"native ids never cross harness domains and a {harness!r} repair is refused"
                )
            if (
                lineage.native_session_id is not None
                and lineage.native_session_id != native_session_id
            ):
                raise StableAgentConflict(
                    f"lineage {lineage.lineage_id} is already bound to native session "
                    f"{lineage.native_session_id!r}; repairing it with "
                    f"{native_session_id!r} would overwrite a known identity"
                )
            if lineage.native_session_id is None:
                lineage.native_session_id = native_session_id
                lineage.acquisition_method = acquisition_method
                if route_provenance is not None:
                    lineage.route_provenance_json = _canonical_json(route_provenance)
                if continuity_note is not None:
                    lineage.continuity_note = continuity_note
                lineage.lineage_origin = LINEAGE_ORIGIN_REPAIR
                lineage.updated_at = _now()
            else:
                lineage = existing if existing is not None else lineage
        else:
            stamp = _now()
            lineage = database.StableAgentLineageModel(
                lineage_id=str(uuid.uuid4()),
                agent_id=agent.agent_id,
                harness=harness,
                native_session_id=native_session_id,
                acquisition_method=acquisition_method,
                route_provenance_json=(
                    _canonical_json(route_provenance) if route_provenance else None
                ),
                continuity_note=continuity_note,
                predecessor_lineage_id=agent.current_lineage_id,
                lineage_origin=LINEAGE_ORIGIN_REPAIR,
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(lineage)
            incarnation.lineage_id = lineage.lineage_id
        incarnation.updated_at = _now()
        agent.current_lineage_id = lineage.lineage_id
        agent.current_incarnation_id = incarnation.incarnation_id
        agent.disposition = _agent_disposition(session, agent, lineage)
        agent.revision = int(agent.revision or 0) + 1
        agent.updated_at = _now()
        session.flush()
        return {
            "agent": _agent_dict(agent),
            "lineage": _lineage_dict(lineage),
            "incarnation": _incarnation_dict(incarnation),
        }

    if db is not None:
        try:
            with db.begin_nested():
                return _repair(db)
        except IntegrityError as exc:
            # A concurrent repairer won the (harness, native_session_id)
            # slot.  The savepoint is rolled back; the caller retries and
            # adopts the winner's lineage on that retry.
            raise StableAgentUnavailable(
                f"concurrent native-identity repair refused; retry to adopt: {exc}"
            ) from exc
    import time

    from sqlalchemy.exc import OperationalError

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                with session.begin_nested():
                    result = _repair(session)
                session.commit()
                return result
        except IntegrityError as exc:
            # The winner's (harness, id) lineage is visible on the retry:
            # exact-adopt or a typed conflict, never a raw driver error.
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05)
    raise StableAgentUnavailable(
        f"concurrent native-identity repair kept conflicting; refusing after retry: {last_error}"
    )


# ---------------------------------------------------------------------------
# read / audit surfaces
# ---------------------------------------------------------------------------


def get_agent(agent_id: str, db: Any = None) -> dict[str, Any]:
    """One stable agent with its current lineage and incarnation embedded."""

    def _get(session: Any) -> dict[str, Any]:
        row = (
            session.query(database.StableAgentModel)
            .filter(database.StableAgentModel.agent_id == agent_id)
            .one_or_none()
        )
        if row is None:
            raise StableAgentNotFound(f"unknown stable agent: {agent_id}")
        record = _agent_dict(row)
        current_lineage = (
            _lineage_by_id(session, row.current_lineage_id)
            if row.current_lineage_id is not None
            else None
        )
        # The pointer is an incarnation_id, never a terminal_id: look the
        # incarnation up by its primary key, not by terminal.
        current_incarnation = None
        if row.current_incarnation_id is not None:
            current_incarnation = (
                session.query(database.StableAgentIncarnationModel)
                .filter(
                    database.StableAgentIncarnationModel.incarnation_id
                    == row.current_incarnation_id
                )
                .one_or_none()
            )
        record["current_lineage"] = _lineage_dict(current_lineage) if current_lineage else None
        record["current_incarnation"] = (
            _incarnation_dict(current_incarnation) if current_incarnation else None
        )
        return record

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def list_agents(session_name: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    """Every stable agent, oldest first; optionally scoped to a session."""

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.StableAgentModel)
        if session_name is not None:
            query = query.filter(database.StableAgentModel.session_name == session_name)
        rows = query.order_by(
            database.StableAgentModel.created_at, database.StableAgentModel.agent_id
        ).all()
        return [_agent_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)


def list_lineages(agent_id: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    """Append-only lineage history, oldest first."""

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.StableAgentLineageModel)
        if agent_id is not None:
            query = query.filter(database.StableAgentLineageModel.agent_id == agent_id)
        rows = query.order_by(
            database.StableAgentLineageModel.created_at, database.StableAgentLineageModel.lineage_id
        ).all()
        return [_lineage_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)


def list_incarnations(agent_id: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    """Physical incarnation history, oldest first."""

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.StableAgentIncarnationModel)
        if agent_id is not None:
            query = query.filter(database.StableAgentIncarnationModel.agent_id == agent_id)
        rows = query.order_by(
            database.StableAgentIncarnationModel.created_at,
            database.StableAgentIncarnationModel.incarnation_id,
        ).all()
        return [_incarnation_dict(row) for row in rows]

    if db is not None:
        return _list(db)
    with database.SessionLocal() as session:
        return _list(session)


def get_incarnation_by_terminal(
    terminal_id: str, generation: Optional[str] = None, db: Any = None
) -> Optional[dict[str, Any]]:
    """The roster incarnation for a terminal, or None (read-only).

    With an exact generation: that generation's incarnation, or None.
    Without one: the unique live incarnation, or None — two live
    incarnations sharing a terminal id refuse (``StableAgentConflict``)
    rather than pick an arbitrary historical row.
    """

    def _get(session: Any) -> Optional[dict[str, Any]]:
        if generation is not None:
            row = _incarnation_by_exact(session, terminal_id, generation)
        else:
            row = _live_incarnation_by_terminal(session, terminal_id)
        return _incarnation_dict(row) if row is not None else None

    if db is not None:
        return _get(db)
    with database.SessionLocal() as session:
        return _get(session)


def audit_dry_run(db: Any = None) -> dict[str, Any]:
    """A truthful, non-crashing roster-wide audit for later migration and
    status repair.  Never mutates; corrupt or unknown rows are reported as
    problems, never fatal."""

    def _audit(session: Any) -> dict[str, Any]:
        agents = session.query(database.StableAgentModel).all()
        lineages = session.query(database.StableAgentLineageModel).all()
        incarnations = session.query(database.StableAgentIncarnationModel).all()
        problems: list[dict[str, Any]] = []
        identity_missing_agents: list[dict[str, Any]] = []
        legacy_candidates: list[dict[str, Any]] = []

        agent_ids = {row.agent_id for row in agents}
        lineage_ids = {row.lineage_id for row in lineages}
        incarnation_by_terminal = {
            row.terminal_id for row in incarnations if row.terminal_id is not None
        }

        for row in agents:
            record = _agent_dict(row)
            if record["disposition"] == DISPOSITION_IDENTITY_MISSING:
                identity_missing_agents.append(record)
            if not record["disposition_known"]:
                problems.append(
                    {
                        "kind": "unknown-disposition",
                        "agent_id": row.agent_id,
                        "detail": f"disposition {row.disposition!r} is not a known roster value",
                    }
                )
            if row.resume_contract_version != RESUME_CONTRACT_VERSION:
                problems.append(
                    {
                        "kind": "unknown-resume-contract",
                        "agent_id": row.agent_id,
                        "detail": (
                            f"resume_contract_version {row.resume_contract_version!r} is not "
                            f"the current {RESUME_CONTRACT_VERSION!r}"
                        ),
                    }
                )
            if row.current_lineage_id is not None and row.current_lineage_id not in lineage_ids:
                problems.append(
                    {
                        "kind": "dangling-current-lineage",
                        "agent_id": row.agent_id,
                        "detail": f"current_lineage_id {row.current_lineage_id!r} has no row",
                    }
                )
            if row.current_incarnation_id is not None and row.current_incarnation_id not in {
                i.incarnation_id for i in incarnations
            }:
                problems.append(
                    {
                        "kind": "dangling-current-incarnation",
                        "agent_id": row.agent_id,
                        "detail": f"current_incarnation_id {row.current_incarnation_id!r} has no row",
                    }
                )
            # Disposition/incarnation consistency (i-0025): a LIVE agent must
            # back its live disposition with a live current incarnation, and
            # a retired current incarnation must be reflected as dormant.
            if row.current_incarnation_id is not None:
                current_inc = next(
                    (i for i in incarnations if i.incarnation_id == row.current_incarnation_id),
                    None,
                )
                if current_inc is not None:
                    if (
                        row.disposition == DISPOSITION_LIVE
                        and current_inc.disposition not in LIVE_INCARNATION_DISPOSITIONS
                    ):
                        problems.append(
                            {
                                "kind": "live-agent-with-retired-current-incarnation",
                                "agent_id": row.agent_id,
                                "detail": (
                                    f"agent disposition is {row.disposition!r} but its "
                                    f"current incarnation is {current_inc.disposition!r}"
                                ),
                            }
                        )
                    if (
                        row.disposition == DISPOSITION_DORMANT
                        and current_inc.disposition in LIVE_INCARNATION_DISPOSITIONS
                    ):
                        problems.append(
                            {
                                "kind": "dormant-agent-with-live-current-incarnation",
                                "agent_id": row.agent_id,
                                "detail": (
                                    f"agent disposition is {row.disposition!r} but its "
                                    f"current incarnation is {current_inc.disposition!r}"
                                ),
                            }
                        )

        for row in lineages:
            if row.agent_id not in agent_ids:
                problems.append(
                    {
                        "kind": "orphan-lineage",
                        "lineage_id": row.lineage_id,
                        "detail": f"agent {row.agent_id!r} has no roster row",
                    }
                )
            if (
                row.route_provenance_json is not None
                and _parse_json(row.route_provenance_json) is None
            ):
                problems.append(
                    {
                        "kind": "corrupt-route-provenance",
                        "lineage_id": row.lineage_id,
                        "detail": "route_provenance_json is not valid JSON",
                    }
                )
            if row.native_session_id is None and row.lineage_origin not in LINEAGE_ORIGINS:
                problems.append(
                    {
                        "kind": "unknown-lineage-origin",
                        "lineage_id": row.lineage_id,
                        "detail": f"lineage_origin {row.lineage_origin!r} is not known",
                    }
                )

        for row in incarnations:
            if row.agent_id not in agent_ids:
                problems.append(
                    {
                        "kind": "orphan-incarnation",
                        "incarnation_id": row.incarnation_id,
                        "detail": f"agent {row.agent_id!r} has no roster row",
                    }
                )
            if row.lineage_id is not None and row.lineage_id not in lineage_ids:
                problems.append(
                    {
                        "kind": "dangling-incarnation-lineage",
                        "incarnation_id": row.incarnation_id,
                        "detail": f"lineage {row.lineage_id!r} has no row",
                    }
                )
            if row.disposition not in INCARNATION_DISPOSITIONS:
                problems.append(
                    {
                        "kind": "unknown-incarnation-disposition",
                        "incarnation_id": row.incarnation_id,
                        "detail": f"disposition {row.disposition!r} is not known",
                    }
                )

        # Legacy migration candidates: terminals that already carry a native
        # session id (the machine-recorded fact) but have no roster
        # incarnation yet.  Read-only; migration itself is a later lane.
        for model, id_attr, native_attr in (
            (database.TerminalModel, "id", "native_session_id"),
            (database.ManagedLaunchV2TerminalModel, "id", "v2_native_session_id"),
        ):
            try:
                rows = session.query(model).all()
            except Exception:  # noqa: BLE001 - a missing table must not crash the audit
                continue
            for row in rows:
                terminal_id = getattr(row, id_attr)
                native_id = getattr(row, native_attr, None)
                if terminal_id in incarnation_by_terminal:
                    continue
                if native_id:
                    legacy_candidates.append(
                        {
                            "terminal_id": terminal_id,
                            "session_name": getattr(row, "tmux_session", None),
                            "provider": getattr(row, "provider", None),
                            "native_session_id": native_id,
                        }
                    )

        return {
            "schema": "cao-m3-roster-audit-v1",
            "agents_total": len(agents),
            "lineages_total": len(lineages),
            "incarnations_total": len(incarnations),
            "live_count": sum(1 for a in agents if a.disposition == DISPOSITION_LIVE),
            "dormant_count": sum(1 for a in agents if a.disposition == DISPOSITION_DORMANT),
            "identity_missing_count": len(identity_missing_agents),
            "identity_missing_agents": identity_missing_agents,
            "legacy_candidates_count": len(legacy_candidates),
            "legacy_candidates": legacy_candidates,
            "problems": problems,
            "problems_count": len(problems),
        }

    if db is not None:
        return _audit(db)
    with database.SessionLocal() as session:
        return _audit(session)
