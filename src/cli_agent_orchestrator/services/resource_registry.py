"""Code-owned generation resource registry (fork-owned side).

One versioned registry (``registry_schema_version = 1``) that every
generation resource constructor, lookup, monitor, cleanup, and deleter
must use — covering logs, scrollback, snapshots, FIFOs, provider
instances, status/screen/timer maps, bridge state/socket trees, tmux
windows/server state, herdr, memory-injection sets, curator locks,
pipe-pane/watchdogs, provider cleanup, plugin callbacks/events,
``session_env``, and old unversioned list/delete paths.

Invariant: the lifecycle is journal-first — the intent row (with the
complete *desired* identity, every chosen path/name embedding the
``entry_id``) commits durably before any physical action; creation is
idempotent and keyed by ``entry_id``; a resource created before its
server-assigned id was captured is *discovered* by the embedded id;
``created``/``active`` require a verified existence receipt;
``aborted`` is lawful only on a verified-absence receipt; drains and
deletes run in reverse dependency order; every mutation is a
``state_seq`` CAS journaled in ``resource_event``.

Failure mode prevented: prose whole-generation inventories omit shared
resources, and register-before-create phantoms / create-before-register
orphans leak tmux windows, sockets, and FIFOs or delete another
generation's resources — the crash-window resource-loss classes.

Why this guard exists: teardown claims, v2-surface isolation, and
rollback drains are only as sound as the enumeration of what a
generation owns; this registry is that enumeration, with the database
itself (not prose) enforcing the topology.

The DDL below is the normative creation script (``:db_uuid`` and
``:created_at`` are supplied at creation; the meta INSERT runs as a
bound statement inside the same transaction).  It requires an SQLite
supporting common table expressions inside trigger programs.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

REGISTRY_SCHEMA_VERSION = 1

RESOURCE_KINDS = (
    "log",
    "scrollback",
    "snapshot",
    "fifo",
    "socket",
    "provider_instance",
    "status_map",
    "screen_map",
    "timer_map",
    "bridge_state",
    "tmux_window",
    "tmux_server_state",
    "herdr",
    "memory_injection",
    "curator_lock",
    "pipe_pane",
    "watchdog",
    "cleanup_hook",
    "plugin_callback",
    "session_env",
    "db_row_set",
    "other",
)
LIFECYCLE_STATES = (
    "declared",
    "created",
    "active",
    "draining",
    "closed",
    "deleted",
    "aborted",
)
OWNERSHIPS = ("owned", "external", "shared")
ROLLBACK_RULES = ("drain-before-rollback", "generation-isolated", "quarantine")

_CREATE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=FULL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA recursive_triggers=ON;",
)

# The normative creation transaction (meta INSERT bound separately with
# the supplied :db_uuid / :created_at values inside the same BEGIN).
_CREATE_SCRIPT = """
CREATE TABLE registry_meta (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE resource (
  entry_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('log','scrollback','snapshot','fifo',
    'socket','provider_instance','status_map','screen_map','timer_map',
    'bridge_state','tmux_window','tmux_server_state','herdr',
    'memory_injection','curator_lock','pipe_pane','watchdog','cleanup_hook',
    'plugin_callback','session_env','db_row_set','other')),
  protocol_vintage TEXT NOT NULL CHECK (protocol_vintage IN ('v1','v2')),
  terminal_id TEXT NOT NULL, generation TEXT NOT NULL,
  owner TEXT NOT NULL CHECK (owner IN ('conductor','fork','shared')),
  ownership TEXT NOT NULL CHECK (ownership IN ('owned','external','shared')),
  desired_fs_path TEXT, desired_db_key TEXT,
  desired_tmux_name TEXT, desired_memory_key TEXT,
  observed_fs_path TEXT, observed_db_key TEXT,
  observed_tmux_id TEXT, observed_pid INTEGER, observed_memory_key TEXT,
  constructor_id TEXT NOT NULL, monitor_id TEXT, deleter_id TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN
    ('declared','created','active','draining','closed','deleted','aborted')),
  state_seq INTEGER NOT NULL CHECK (state_seq >= 1),
  proof_receipt_digest TEXT CHECK (proof_receipt_digest IS NULL
    OR length(proof_receipt_digest)=64),
  rollback_rule TEXT NOT NULL CHECK (rollback_rule IN
    ('drain-before-rollback','generation-isolated','quarantine')),
  CHECK (desired_fs_path IS NOT NULL OR desired_db_key IS NOT NULL
         OR desired_tmux_name IS NOT NULL OR desired_memory_key IS NOT NULL)
);
CREATE UNIQUE INDEX resource_live_desired_fs ON resource(desired_fs_path)
  WHERE desired_fs_path IS NOT NULL
    AND lifecycle_state IN ('declared','created','active','draining');
CREATE UNIQUE INDEX resource_live_desired_db ON resource(desired_db_key)
  WHERE desired_db_key IS NOT NULL
    AND lifecycle_state IN ('declared','created','active','draining');
CREATE UNIQUE INDEX resource_live_desired_tmux ON resource(desired_tmux_name)
  WHERE desired_tmux_name IS NOT NULL
    AND lifecycle_state IN ('declared','created','active','draining');
CREATE UNIQUE INDEX resource_live_desired_mem ON resource(desired_memory_key)
  WHERE desired_memory_key IS NOT NULL
    AND lifecycle_state IN ('declared','created','active','draining');
CREATE UNIQUE INDEX resource_live_observed_tmux ON resource(observed_tmux_id)
  WHERE observed_tmux_id IS NOT NULL
    AND lifecycle_state IN ('created','active','draining');
CREATE TABLE resource_dependency (
  entry_id TEXT NOT NULL REFERENCES resource(entry_id),
  depends_on_entry_id TEXT NOT NULL REFERENCES resource(entry_id),
  PRIMARY KEY (entry_id, depends_on_entry_id),
  CHECK (entry_id <> depends_on_entry_id)
) WITHOUT ROWID;
CREATE TRIGGER rd_no_cycle BEFORE INSERT ON resource_dependency
WHEN EXISTS (
  WITH RECURSIVE reach(id) AS (
    SELECT NEW.depends_on_entry_id
    UNION SELECT rd.depends_on_entry_id FROM resource_dependency rd
      JOIN reach ON rd.entry_id = reach.id)
  SELECT 1 FROM reach WHERE id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT,'dependency cycle refused'); END;
CREATE TABLE resource_event (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id TEXT NOT NULL REFERENCES resource(entry_id),
  from_state TEXT, to_state TEXT NOT NULL,
  state_seq INTEGER NOT NULL,
  actor_id TEXT NOT NULL, at TEXT NOT NULL,
  evidence_digest TEXT CHECK (evidence_digest IS NULL
    OR length(evidence_digest)=64)
);
CREATE TRIGGER re_no_update BEFORE UPDATE ON resource_event
  BEGIN SELECT RAISE(ABORT,'resource_event is append-only'); END;
CREATE TRIGGER re_no_delete BEFORE DELETE ON resource_event
  BEGIN SELECT RAISE(ABORT,'resource_event is append-only'); END;
CREATE TRIGGER resource_state_cas BEFORE UPDATE ON resource
WHEN NEW.state_seq <> OLD.state_seq + 1
BEGIN SELECT RAISE(ABORT,'resource update must CAS state_seq'); END;
CREATE TRIGGER resource_terminal BEFORE UPDATE ON resource
WHEN OLD.lifecycle_state IN ('deleted','aborted')
BEGIN SELECT RAISE(ABORT,'terminal resource row is immutable'); END;
CREATE TRIGGER resource_no_delete BEFORE DELETE ON resource
BEGIN SELECT RAISE(ABORT,'resource rows are history; never deleted'); END;
CREATE TRIGGER rd_no_update BEFORE UPDATE ON resource_dependency
  BEGIN SELECT RAISE(ABORT,'resource_dependency is append-only'); END;
CREATE TRIGGER rd_no_delete BEFORE DELETE ON resource_dependency
  BEGIN SELECT RAISE(ABORT,'resource_dependency is append-only'); END;
CREATE TABLE resource_consumer (
  entry_id TEXT NOT NULL REFERENCES resource(entry_id),
  consumer_id TEXT NOT NULL,
  PRIMARY KEY (entry_id, consumer_id)
) WITHOUT ROWID;
CREATE TRIGGER rc_no_update BEFORE UPDATE ON resource_consumer
  BEGIN SELECT RAISE(ABORT,'resource_consumer is append-only'); END;
CREATE TRIGGER rc_no_delete BEFORE DELETE ON resource_consumer
  BEGIN SELECT RAISE(ABORT,'resource_consumer is append-only'); END;
"""

# Legal lifecycle transitions; anything else is refused with zero mutation.
_LEGAL = frozenset(
    {
        ("declared", "created"),
        ("declared", "aborted"),
        ("created", "active"),
        ("active", "draining"),
        ("created", "draining"),
        ("draining", "closed"),
        ("closed", "deleted"),
    }
)


class RegistryError(RuntimeError):
    """Base error for resource-registry operations."""


class RegistryNotFound(RegistryError):
    pass


class RegistryTransitionRefused(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ResourceRegistry:
    """The fork-owned durable resource registry (one DB per side)."""

    def __init__(self, db_path: Path, *, db_uuid: Optional[str] = None) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent first-open constructors race on the create
        # transaction; creation is idempotent, so a locked loser retries.
        last_error: Optional[sqlite3.Error] = None
        for _ in range(20):
            conn = self._connect(create=True)
            try:
                for pragma in _CREATE_PRAGMAS:
                    conn.execute(pragma)
                conn.execute("BEGIN")
                conn.executescript(_CREATE_SCRIPT)
                conn.execute(
                    "INSERT OR IGNORE INTO registry_meta(k,v) VALUES "
                    "('registry_schema_version','1'),('db_uuid',?),('created_at',?)",
                    (db_uuid or str(uuid.uuid4()), _now()),
                )
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                conn.rollback()
                last_error = exc
                import time

                time.sleep(0.05)
            except sqlite3.Error as exc:
                conn.rollback()
                raise RegistryError(f"registry creation failed: {exc}") from exc
            finally:
                conn.close()
        else:
            raise RegistryError(f"registry creation failed under contention: {last_error}")
        os.chmod(self._path, 0o600)
        self._verify_meta()

    def _verify_meta(self) -> None:
        conn = self._connect()
        try:
            rows = dict(conn.execute("SELECT k, v FROM registry_meta"))
        finally:
            conn.close()
        # A registry whose meta rows are absent/unknown is refused —
        # never treated as empty-and-new.
        if rows.get("registry_schema_version") != str(REGISTRY_SCHEMA_VERSION):
            raise RegistryError("registry meta rows are absent or name an unknown schema version")
        if not rows.get("db_uuid") or not rows.get("created_at"):
            raise RegistryError("registry meta rows are incomplete")

    def _connect(self, create: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA recursive_triggers=ON")
        if not create:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() != "wal":
                raise RegistryError(f"registry journal_mode must be WAL, got {mode}")
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ reads

    def _row(self, conn: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM resource WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            raise RegistryNotFound(f"no registry entry: {entry_id}")
        return row

    def resolve(self, entry_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = self._row(conn, entry_id)
            return self._entry_dict(conn, row)
        finally:
            conn.close()

    def _entry_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        entry = dict(row)
        entry["depends_on"] = [
            r[0]
            for r in conn.execute(
                "SELECT depends_on_entry_id FROM resource_dependency WHERE entry_id=? "
                "ORDER BY depends_on_entry_id",
                (row["entry_id"],),
            )
        ]
        entry["consumer_ids"] = [
            r[0]
            for r in conn.execute(
                "SELECT consumer_id FROM resource_consumer WHERE entry_id=? "
                "ORDER BY consumer_id",
                (row["entry_id"],),
            )
        ]
        entry["events"] = [
            dict(event)
            for event in conn.execute(
                "SELECT from_state, to_state, state_seq, actor_id, at, evidence_digest "
                "FROM resource_event WHERE entry_id=? ORDER BY event_seq",
                (row["entry_id"],),
            )
        ]
        return entry

    def enumerate(
        self,
        *,
        terminal_id: Optional[str] = None,
        generation: Optional[str] = None,
        lifecycle_states: Optional[tuple[str, ...]] = None,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            query = "SELECT * FROM resource WHERE 1=1"
            params: list[Any] = []
            if terminal_id is not None:
                query += " AND terminal_id=?"
                params.append(terminal_id)
            if generation is not None:
                query += " AND generation=?"
                params.append(generation)
            if lifecycle_states:
                query += " AND lifecycle_state IN (%s)" % ",".join("?" for _ in lifecycle_states)
                params.extend(lifecycle_states)
            return [self._entry_dict(conn, row) for row in conn.execute(query, params)]
        finally:
            conn.close()

    # ------------------------------------------------------------ writes

    def declare(
        self,
        *,
        entry_id: str,
        kind: str,
        protocol_vintage: str,
        terminal_id: str,
        generation: str,
        owner: str,
        ownership: str,
        constructor_id: str,
        deleter_id: str,
        rollback_rule: str,
        desired_fs_path: Optional[str] = None,
        desired_db_key: Optional[str] = None,
        desired_tmux_name: Optional[str] = None,
        desired_memory_key: Optional[str] = None,
        monitor_id: Optional[str] = None,
        depends_on: tuple[str, ...] = (),
        consumer_ids: tuple[str, ...] = (),
        actor_id: str,
    ) -> dict[str, Any]:
        """Commit the intent row durably before any physical action.

        For ``ownership=owned`` every chosen desired path/name must embed
        the ``entry_id`` so a crash between physical creation and
        observed-id capture stays recoverable by searching for it;
        ``external``/``shared`` monitor-only entries record the
        preexisting identity verbatim and are never created or deleted.
        """
        if kind not in RESOURCE_KINDS:
            raise RegistryError(f"unknown resource kind: {kind!r}")
        if protocol_vintage not in ("v1", "v2"):
            raise RegistryError("protocol_vintage must be v1 or v2")
        if ownership not in OWNERSHIPS:
            raise RegistryError(f"ownership must be one of {OWNERSHIPS}")
        if rollback_rule not in ROLLBACK_RULES:
            raise RegistryError(f"rollback_rule must be one of {ROLLBACK_RULES}")
        desired = (desired_fs_path, desired_db_key, desired_tmux_name, desired_memory_key)
        if not any(desired):
            raise RegistryError("a declaration requires at least one desired identity")
        if ownership == "owned":
            for value in desired:
                if value is not None and entry_id not in value:
                    raise RegistryError(
                        "owned desired identities must embed the entry_id so the "
                        "resource stays discoverable across the create-to-capture "
                        "crash window"
                    )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for dependency in depends_on:
                self._row(conn, dependency)
            conn.execute(
                "INSERT INTO resource(entry_id, kind, protocol_vintage, terminal_id, "
                "generation, owner, ownership, desired_fs_path, desired_db_key, "
                "desired_tmux_name, desired_memory_key, constructor_id, monitor_id, "
                "deleter_id, lifecycle_state, state_seq, rollback_rule) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'declared',1,?)",
                (
                    entry_id,
                    kind,
                    protocol_vintage,
                    terminal_id,
                    generation,
                    owner,
                    ownership,
                    desired_fs_path,
                    desired_db_key,
                    desired_tmux_name,
                    desired_memory_key,
                    constructor_id,
                    monitor_id,
                    deleter_id,
                    rollback_rule,
                ),
            )
            for dependency in depends_on:
                conn.execute(
                    "INSERT INTO resource_dependency(entry_id, depends_on_entry_id) "
                    "VALUES (?,?)",
                    (entry_id, dependency),
                )
            for consumer in consumer_ids:
                conn.execute(
                    "INSERT INTO resource_consumer(entry_id, consumer_id) VALUES (?,?)",
                    (entry_id, consumer),
                )
            conn.execute(
                "INSERT INTO resource_event(entry_id, from_state, to_state, state_seq, "
                "actor_id, at) VALUES (?,NULL,'declared',1,?,?)",
                (entry_id, actor_id, _now()),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise RegistryConflict(f"declaration conflicts with live state: {exc}") from exc
        except sqlite3.Error as exc:
            conn.rollback()
            raise RegistryError(f"declaration failed: {exc}") from exc
        finally:
            conn.close()
        return self.resolve(entry_id)

    def _transition(
        self,
        entry_id: str,
        to_state: str,
        *,
        actor_id: str,
        evidence_digest: Optional[str] = None,
        observed: Optional[dict[str, Any]] = None,
        proof_receipt_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, entry_id)
            from_state = row["lifecycle_state"]
            if from_state == to_state:
                # Idempotent re-drive of a transition (crash between the
                # effect and the journal append): converge, never duplicate.
                if observed:
                    self._apply_observed(conn, row, observed)
                conn.commit()
                return self._entry_dict(conn, self._row(conn, entry_id))
            if (from_state, to_state) not in _LEGAL:
                raise RegistryTransitionRefused(
                    f"illegal lifecycle transition {from_state!r} -> {to_state!r}"
                )
            if to_state == "active":
                for dependency in conn.execute(
                    "SELECT depends_on_entry_id FROM resource_dependency WHERE entry_id=?",
                    (entry_id,),
                ):
                    dep_row = self._row(conn, dependency[0])
                    if dep_row["lifecycle_state"] != "active":
                        raise RegistryTransitionRefused(
                            f"dependency {dependency[0]} is not active "
                            f"({dep_row['lifecycle_state']})"
                        )
            updates = {
                "lifecycle_state": to_state,
                "state_seq": row["state_seq"] + 1,
            }
            if observed:
                for key, value in observed.items():
                    if key not in (
                        "observed_fs_path",
                        "observed_db_key",
                        "observed_tmux_id",
                        "observed_pid",
                        "observed_memory_key",
                    ):
                        raise RegistryError(f"unknown observed identity field: {key}")
                    updates[key] = value
            if proof_receipt_digest is not None:
                updates["proof_receipt_digest"] = proof_receipt_digest
            assignments = ", ".join(f"{key}=?" for key in updates)
            conn.execute(
                f"UPDATE resource SET {assignments} WHERE entry_id=?",
                (*updates.values(), entry_id),
            )
            conn.execute(
                "INSERT INTO resource_event(entry_id, from_state, to_state, state_seq, "
                "actor_id, at, evidence_digest) VALUES (?,?,?,?,?,?,?)",
                (
                    entry_id,
                    from_state,
                    to_state,
                    row["state_seq"] + 1,
                    actor_id,
                    _now(),
                    evidence_digest,
                ),
            )
            conn.commit()
            return self._entry_dict(conn, self._row(conn, entry_id))
        except (RegistryError, sqlite3.IntegrityError) as exc:
            conn.rollback()
            if isinstance(exc, sqlite3.IntegrityError):
                raise RegistryConflict(str(exc)) from exc
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise RegistryError(f"transition failed: {exc}") from exc
        finally:
            conn.close()

    def _apply_observed(
        self, conn: sqlite3.Connection, row: sqlite3.Row, observed: dict[str, Any]
    ) -> None:
        updates = {key: value for key, value in observed.items()}
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        # state_seq still CASes (+1) on an observed-identity capture.
        conn.execute(
            f"UPDATE resource SET {assignments}, state_seq=? WHERE entry_id=?",
            (*updates.values(), row["state_seq"] + 1, row["entry_id"]),
        )

    def register_created(
        self,
        entry_id: str,
        *,
        actor_id: str,
        observed: Optional[dict[str, Any]] = None,
        existence_receipt_digest: str,
    ) -> dict[str, Any]:
        """Mark physical creation verified (existence receipt required)."""
        if len(existence_receipt_digest) != 64:
            raise RegistryError("an existence receipt digest (64 hex) is required")
        return self._transition(
            entry_id,
            "created",
            actor_id=actor_id,
            evidence_digest=existence_receipt_digest,
            observed=observed,
        )

    def activate(
        self, entry_id: str, *, actor_id: str, existence_receipt_digest: str
    ) -> dict[str, Any]:
        """Mark active: every dependency must already be active."""
        return self._transition(
            entry_id,
            "active",
            actor_id=actor_id,
            evidence_digest=existence_receipt_digest,
        )

    def discover(
        self,
        entry_id: str,
        *,
        actor_id: str,
        finder: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Restart reconciliation for the create-to-capture crash window.

        ``finder`` searches for the resource by its embedded ``entry_id``
        (tmux name/format tag, path, key) and returns the observed
        identity (or None when no trace exists).  A declaration with no
        physical trace is left ``declared`` for re-create or abort; a
        found resource records its observed identity by state_seq CAS.
        """
        entry = self.resolve(entry_id)
        found = finder(entry)
        if found is None:
            return entry
        if entry["lifecycle_state"] not in ("declared", "created"):
            raise RegistryTransitionRefused(
                f"discovery is lawful only from declared/created, not "
                f"{entry['lifecycle_state']!r}"
            )
        return self._transition(
            entry_id,
            "created",
            actor_id=actor_id,
            observed=found,
            evidence_digest=None,
        )

    def drain(self, entry_id: str, *, actor_id: str) -> dict[str, Any]:
        return self._transition(entry_id, "draining", actor_id=actor_id)

    def close(self, entry_id: str, *, actor_id: str) -> dict[str, Any]:
        return self._transition(entry_id, "closed", actor_id=actor_id)

    def delete(
        self,
        entry_id: str,
        *,
        actor_id: str,
        verified_absence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        """Physical removal happened first and was verified; row retained."""
        return self._transition(
            entry_id, "deleted", actor_id=actor_id, evidence_digest=verified_absence_digest
        )

    def abort(
        self, entry_id: str, *, actor_id: str, verified_absence_digest: str
    ) -> dict[str, Any]:
        """Abandon a declaration — lawful only on a verified-absence receipt.

        A probe that finds any trace must advance or quarantine, never
        abort; the caller supplies the digest of the empty-probe receipt.
        """
        if len(verified_absence_digest) != 64:
            raise RegistryError("abort requires a verified-absence receipt digest")
        return self._transition(
            entry_id, "aborted", actor_id=actor_id, evidence_digest=verified_absence_digest
        )

    def monitor(self, entry_id: str, *, actor_id: str, monitor_id: str) -> dict[str, Any]:
        """Attach a monitor to an external/shared entry (never creates)."""
        entry = self.resolve(entry_id)
        if entry["ownership"] == "owned":
            raise RegistryTransitionRefused(
                "monitor() is for external/shared entries; owned entries are created"
            )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, entry_id)
            conn.execute(
                "UPDATE resource SET monitor_id=?, state_seq=? WHERE entry_id=?",
                (monitor_id, row["state_seq"] + 1, entry_id),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise RegistryError(f"monitor update failed: {exc}") from exc
        finally:
            conn.close()
        return self.resolve(entry_id)

    def drain_order(self, *, terminal_id: str, generation: str) -> list[dict[str, Any]]:
        """Live entries of one generation in reverse dependency order.

        Dependents drain before the entries they depend on; the
        dependency graph is acyclic by trigger, so the sort is total.
        """
        entries = self.enumerate(
            terminal_id=terminal_id,
            generation=generation,
            lifecycle_states=("declared", "created", "active", "draining"),
        )
        by_id = {entry["entry_id"]: entry for entry in entries}
        ordered: list[dict[str, Any]] = []
        visited: dict[str, int] = {}

        def visit(entry_id: str) -> None:
            state = visited.get(entry_id)
            if state == 2:
                return
            if state == 1:
                raise RegistryError("dependency cycle encountered during drain sort")
            visited[entry_id] = 1
            entry = by_id[entry_id]
            for dependent in entries:
                if entry_id in dependent["depends_on"]:
                    visit(dependent["entry_id"])
            visited[entry_id] = 2
            ordered.append(entry)

        for entry in entries:
            visit(entry["entry_id"])
        return ordered
