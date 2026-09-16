"""Private, integrity-checked executable material for ``plan-v2``.

This module is intentionally not imported by any public API surface. Public approval
documents carry only the digests in :mod:`plan_identifier`; this store holds the raw
pre-redaction bytes and returns them only after every byte string matches that public
contract.

The 256 KiB total is an explicit exact-material ceiling. Per-scope memory's
separate 1000-character budget belongs to its later producer, not this store.
Owner-only POSIX mode checks close accidental same-host disclosure to other
users; they do not protect against the same OS user, and ``chmod`` is not a
security control on non-POSIX filesystems. A separate database is deliberately
not used because run deletion and final-reference garbage collection must share
one SQLite transaction. General database startup permission repair remains
best-effort for compatibility; this private-byte boundary is stricter: writes
must secure exposed files first, while reads refuse exposed files without
changing their modes. Reads use SQLite's read-only URI mode and never migrate
or recover a hot journal; inability to read without recovery fails closed.
Release is different from disclosure: it tries to tighten permissions, but
still deletes rows with one safe warning when tightening is impossible so
private material is not retained indefinitely. Row deletion is not a secure
filesystem-erasure guarantee.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES
from cli_agent_orchestrator.services import plan_identifier

logger = logging.getLogger(__name__)

_STRUCTURED_MATERIALS = frozenset(
    {"declaration", "targets", "limits", "retry_policy", "policy", "memory"}
)
COMPONENT_MATERIAL_KINDS = {
    "artifact_hash": "source-bytes",
    **{name: "json-utf8" for name in _STRUCTURED_MATERIALS},
}


class PrivateSnapshotError(RuntimeError):
    """Base class whose messages never contain private snapshot bytes or paths."""


class PrivateSnapshotIntegrityError(PrivateSnapshotError):
    """Private bytes are absent or do not match the approved component digests."""

    def __init__(self, component: str, condition: str = "is invalid") -> None:
        super().__init__(
            f"private snapshot component {component} {condition}; start a new approved run"
        )


class PrivateSnapshotTooLargeError(PrivateSnapshotError):
    """Exact executable material exceeded its hard ceiling and was not truncated."""

    def __init__(self, component: str, limit: int) -> None:
        super().__init__(
            f"private snapshot component {component} exceeds {limit} bytes; "
            "executable material was refused"
        )


class PrivateSnapshotStoreError(PrivateSnapshotError):
    """The private store could not be opened or updated safely."""

    def __init__(self, cause: str = "unavailable") -> None:
        super().__init__(f"private snapshot store {cause}")


class PrivateSnapshotPermissionsError(PrivateSnapshotStoreError):
    """The shared database is exposed beyond its owner."""

    def __init__(self) -> None:
        super().__init__("not owner-only; restrict database file")


@dataclass(frozen=True)
class VerifiedPrivateSnapshot:
    """Raw bytes returned only after full digest verification."""

    plan_id: str
    components: tuple[tuple[str, bytes], ...]

    def material(self, component: str) -> bytes:
        for name, content in self.components:
            if name == component:
                return content
        raise PrivateSnapshotIntegrityError(component, "is missing")

    def decode_material(self, component: str) -> Any:
        """Decode one already integrity-checked component by its fixed kind."""
        return decode_material(component, self.material(component))


def structured_material_bytes(value: Any) -> bytes:
    """Serialize one structured private component with the sole JSON contract."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise PrivateSnapshotIntegrityError("structured material") from None


def decode_material(name: str, material: bytes) -> Any:
    """Decode a verified material according to the fixed producer-kind table.

    JSON callers must obtain ``material`` from :class:`VerifiedPrivateSnapshot`;
    this function does not perform storage integrity verification itself.
    """
    if not isinstance(name, str) or not isinstance(material, bytes):
        raise PrivateSnapshotIntegrityError("material")
    kind = COMPONENT_MATERIAL_KINDS.get(name)
    try:
        if kind == "source-bytes":
            return material
        if kind == "json-utf8":
            value = json.loads(material.decode("utf-8"))
            if structured_material_bytes(value) != material:
                raise PrivateSnapshotIntegrityError(name)
            return value
        if name.startswith("input-key:"):
            value = material.decode("utf-8")
            if plan_identifier.canonical_key_bytes(value) != material:
                raise PrivateSnapshotIntegrityError("input key")
            return value
        if name.startswith("input-value:"):
            return plan_identifier.decode_component_bytes(material)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        plan_identifier.PlanV2ComponentError,
    ):
        raise PrivateSnapshotIntegrityError(name) from None
    raise PrivateSnapshotIntegrityError("material kind")


def _expected_digests(
    components: plan_identifier.PlanV2Components,
) -> dict[str, str]:
    plan_identifier.compute_v2(components)
    expected = {
        "artifact_hash": components.artifact_hash,
        "declaration": components.declaration,
        "targets": components.targets,
    }
    for item in components.inputs:
        expected[f"input-key:{item.key_digest}"] = item.key_digest
        expected[f"input-value:{item.key_digest}"] = item.value_digest
    expected.update(
        {
            "limits": components.limits,
            "retry_policy": components.retry_policy,
            "policy": components.policy,
            "memory": components.memory,
        }
    )
    return expected


def _validated_materials(
    components: plan_identifier.PlanV2Components, materials: Any
) -> tuple[str, dict[str, bytes], dict[str, str]]:
    expected = _expected_digests(components)
    if not isinstance(materials, dict) or set(materials) != set(expected):
        raise PrivateSnapshotIntegrityError("set")
    checked: dict[str, bytes] = {}
    total = 0
    for name, digest in expected.items():
        content = materials[name]
        if not isinstance(content, bytes):
            raise PrivateSnapshotIntegrityError(name)
        size = len(content)
        if size > WORKFLOW_MANIFEST_MAX_BYTES:
            raise PrivateSnapshotTooLargeError(name, WORKFLOW_MANIFEST_MAX_BYTES)
        total += size
        if plan_identifier.digest_bytes(content) != digest:
            raise PrivateSnapshotIntegrityError(name)
        checked[name] = content
    if total > WORKFLOW_MANIFEST_MAX_BYTES:
        raise PrivateSnapshotTooLargeError("total snapshot", WORKFLOW_MANIFEST_MAX_BYTES)
    return plan_identifier.compute_v2(components), checked, expected


def _database_files(path: Path) -> tuple[Path, Path, Path, Path]:
    return (
        path,
        path.with_name(path.name + "-journal"),
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    )


def _tighten_permissions(path: Path) -> bool:
    """Best-effort POSIX tightening for the database and existing sidecars."""
    if os.name != "posix":
        return True
    for candidate in _database_files(path):
        try:
            if not candidate.exists():
                continue
            mode = candidate.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(candidate, mode & ~0o077)
            if candidate.stat().st_mode & 0o077:
                return False
        except OSError:
            return False
    return True


def _repair_write_permissions(path: Path) -> None:
    """Tighten exposed files before any private snapshot byte is stored."""
    if not _tighten_permissions(path):
        raise PrivateSnapshotPermissionsError() from None


def _verify_read_permissions(path: Path) -> None:
    """Refuse exposed private storage without mutating filesystem state."""
    if os.name != "posix":
        return
    for candidate in _database_files(path):
        try:
            if candidate.exists() and candidate.stat().st_mode & 0o077:
                raise PrivateSnapshotPermissionsError()
        except OSError:
            raise PrivateSnapshotPermissionsError() from None


def _connection_path(conn: sqlite3.Connection) -> Path:
    database = conn.execute("PRAGMA database_list").fetchone()
    if database is None or not database[2]:
        raise PrivateSnapshotStoreError()
    return Path(database[2])


def _connect() -> sqlite3.Connection:
    from cli_agent_orchestrator.clients.database import _migrate_workflow_plan_snapshot
    from cli_agent_orchestrator.constants import (
        DATABASE_FILE,
        WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS,
    )

    path = Path(DATABASE_FILE)
    try:
        _migrate_workflow_plan_snapshot()
        conn = sqlite3.connect(str(path))
        conn.execute(f"PRAGMA busy_timeout = {WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS}")
        return conn
    except PrivateSnapshotStoreError:
        if "conn" in locals():
            conn.close()
        raise
    except (OSError, sqlite3.Error):
        if "conn" in locals():
            conn.close()
        raise PrivateSnapshotStoreError() from None


def _connect_readonly() -> sqlite3.Connection:
    """Open the existing store read-only without migrations or filesystem writes."""
    from cli_agent_orchestrator.constants import (
        DATABASE_FILE,
        WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS,
    )

    path = Path(DATABASE_FILE)
    try:
        path.stat()
    except FileNotFoundError:
        raise PrivateSnapshotIntegrityError("run reference", "is missing") from None
    except OSError:
        raise PrivateSnapshotStoreError() from None
    _verify_read_permissions(path)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        conn.execute(f"PRAGMA busy_timeout = {WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS}")
        return conn
    except (OSError, ValueError, sqlite3.Error):
        if conn is not None:
            conn.close()
        raise PrivateSnapshotStoreError() from None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _store_or_verify(
    conn: sqlite3.Connection,
    plan_id: str,
    components: plan_identifier.PlanV2Components,
    checked: dict[str, bytes],
    expected: dict[str, str],
) -> None:
    existing = conn.execute(
        "SELECT component_set_version FROM workflow_plan_snapshot WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO workflow_plan_snapshot (plan_id, component_set_version) VALUES (?, ?)",
            (plan_id, plan_identifier.PLAN_V2_COMPONENT_SET_VERSION),
        )
        conn.executemany(
            "INSERT INTO workflow_plan_snapshot_component "
            "(plan_id, component_name, content_digest, content) VALUES (?, ?, ?, ?)",
            [(plan_id, name, expected[name], checked[name]) for name in sorted(checked)],
        )
    else:
        _load_verified(conn, plan_id, components)


def _require_run(conn: sqlite3.Connection, run_id: str) -> None:
    if not _table_exists(conn, "workflow_run"):
        raise PrivateSnapshotIntegrityError("run reference")
    if conn.execute("SELECT 1 FROM workflow_run WHERE run_id = ?", (run_id,)).fetchone() is None:
        raise PrivateSnapshotIntegrityError("run reference")


def _attach(conn: sqlite3.Connection, run_id: str, plan_id: str) -> None:
    existing = conn.execute(
        "SELECT plan_id FROM workflow_run_plan_snapshot WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if existing is not None and existing[0] != plan_id:
        raise PrivateSnapshotIntegrityError("run reference")
    conn.execute(
        "INSERT OR IGNORE INTO workflow_run_plan_snapshot (run_id, plan_id) VALUES (?, ?)",
        (run_id, plan_id),
    )


def freeze_and_attach(
    run_id: str,
    components: plan_identifier.PlanV2Components,
    materials: Any,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Atomically validate, freeze, and attach exact bytes to an existing run.

    With no connection this owns one ``BEGIN IMMEDIATE`` transaction. A caller
    creating an approved run may pass its already-active transaction so the run
    row and private material commit or roll back together; this function never
    commits or rolls back a borrowed connection.
    """
    plan_id, checked, expected = _validated_materials(components, materials)

    def _freeze(active: sqlite3.Connection) -> None:
        _require_run(active, run_id)
        _store_or_verify(active, plan_id, components, checked, expected)
        _attach(active, run_id, plan_id)

    if conn is not None:
        try:
            if not conn.in_transaction:
                raise PrivateSnapshotStoreError("transaction required")
            _repair_write_permissions(_connection_path(conn))
            _freeze(conn)
        except (PrivateSnapshotError, plan_identifier.PlanV2ComponentError):
            raise
        except (OSError, sqlite3.Error):
            raise PrivateSnapshotStoreError() from None
        return plan_id

    try:
        with closing(_connect()) as own:
            _repair_write_permissions(_connection_path(own))
            with own:
                own.execute("BEGIN IMMEDIATE")
                _freeze(own)
    except (PrivateSnapshotError, plan_identifier.PlanV2ComponentError):
        raise
    except sqlite3.Error:
        raise PrivateSnapshotStoreError() from None
    return plan_id


def _load_verified(
    conn: sqlite3.Connection,
    plan_id: str,
    components: plan_identifier.PlanV2Components,
) -> VerifiedPrivateSnapshot:
    expected_plan_id = plan_identifier.compute_v2(components)
    if plan_id != expected_plan_id:
        raise PrivateSnapshotIntegrityError("plan_id")
    header = conn.execute(
        "SELECT component_set_version FROM workflow_plan_snapshot WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if header is None:
        raise PrivateSnapshotIntegrityError("snapshot", "is missing")
    if header[0] != plan_identifier.PLAN_V2_COMPONENT_SET_VERSION:
        raise PrivateSnapshotIntegrityError("component_set_version")
    expected = _expected_digests(components)
    rows = conn.execute(
        "SELECT component_name, content_digest, content "
        "FROM workflow_plan_snapshot_component WHERE plan_id = ? "
        "ORDER BY component_name",
        (plan_id,),
    ).fetchall()
    found = {}
    for name, stored_digest, raw_content in rows:
        if not isinstance(name, str) or name not in expected:
            raise PrivateSnapshotIntegrityError("set")
        if not isinstance(stored_digest, str) or not isinstance(
            raw_content, (bytes, bytearray, memoryview)
        ):
            raise PrivateSnapshotIntegrityError(name)
        found[name] = (stored_digest, bytes(raw_content))
    if set(found) != set(expected):
        missing = next((name for name in expected if name not in found), "set")
        raise PrivateSnapshotIntegrityError(missing, "is missing")
    verified = []
    for name in sorted(expected):
        stored_digest, content = found[name]
        if (
            stored_digest != expected[name]
            or plan_identifier.digest_bytes(content) != expected[name]
        ):
            raise PrivateSnapshotIntegrityError(name)
        verified.append((name, content))
    return VerifiedPrivateSnapshot(plan_id=plan_id, components=tuple(verified))


def _components_from_stored_digests(
    tier: Any, rows: list[tuple[Any, Any]]
) -> plan_identifier.PlanV2Components:
    fixed_names = frozenset(COMPONENT_MATERIAL_KINDS)
    fixed: dict[str, str] = {}
    input_keys: dict[str, str] = {}
    input_values: dict[str, str] = {}
    seen_names: set[str] = set()

    for name, digest in rows:
        if not isinstance(name, str) or not isinstance(digest, str) or name in seen_names:
            raise PrivateSnapshotIntegrityError("component set")
        seen_names.add(name)
        if name in fixed_names:
            fixed[name] = digest
            continue
        if name.startswith("input-key:"):
            key_digest = name.removeprefix("input-key:")
            if digest != key_digest:
                raise PrivateSnapshotIntegrityError("input set")
            input_keys[key_digest] = digest
            continue
        if name.startswith("input-value:"):
            key_digest = name.removeprefix("input-value:")
            input_values[key_digest] = digest
            continue
        raise PrivateSnapshotIntegrityError("component set")

    if set(fixed) != fixed_names:
        raise PrivateSnapshotIntegrityError("component set")
    if set(input_keys) != set(input_values):
        raise PrivateSnapshotIntegrityError("input set")

    return plan_identifier.PlanV2Components(
        tier=tier,
        artifact_hash=fixed["artifact_hash"],
        declaration=fixed["declaration"],
        targets=fixed["targets"],
        inputs=tuple(
            plan_identifier.InputDigest(
                key_digest=key_digest,
                value_digest=input_values[key_digest],
            )
            for key_digest in sorted(input_keys)
        ),
        limits=fixed["limits"],
        retry_policy=fixed["retry_policy"],
        policy=fixed["policy"],
        memory=fixed["memory"],
    )


def stored_plan_components_for_run(
    run_id: str,
) -> tuple[str, plan_identifier.PlanV2Components]:
    """Reconstruct one run's approved identity from stored digest rows only.

    This read does not inspect or verify private component BLOBs. Call
    :func:`private_snapshot_for_run` with the returned components to perform
    that separate byte-integrity check.
    """
    if not isinstance(run_id, str):
        raise PrivateSnapshotIntegrityError("run reference")
    try:
        run_id.encode("utf-8")
    except UnicodeEncodeError:
        raise PrivateSnapshotIntegrityError("run reference") from None
    try:
        with closing(_connect_readonly()) as conn:
            conn.execute("BEGIN")
            try:
                if not all(
                    _table_exists(conn, table)
                    for table in (
                        "workflow_run",
                        "workflow_run_plan_snapshot",
                        "workflow_plan_snapshot",
                        "workflow_plan_snapshot_component",
                    )
                ):
                    raise PrivateSnapshotIntegrityError("run reference", "is missing")

                links = conn.execute(
                    "SELECT plan_id FROM workflow_run_plan_snapshot WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                tiers = conn.execute(
                    "SELECT tier FROM workflow_run WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                if len(links) != 1 or len(tiers) != 1:
                    raise PrivateSnapshotIntegrityError("run reference", "is missing")
                plan_id = links[0][0]
                tier = tiers[0][0]
                if not isinstance(plan_id, str):
                    raise PrivateSnapshotIntegrityError("run reference")

                headers = conn.execute(
                    "SELECT component_set_version FROM workflow_plan_snapshot " "WHERE plan_id = ?",
                    (plan_id,),
                ).fetchall()
                if headers != [(plan_identifier.PLAN_V2_COMPONENT_SET_VERSION,)]:
                    raise PrivateSnapshotIntegrityError("component_set_version")

                rows = conn.execute(
                    "SELECT component_name, content_digest "
                    "FROM workflow_plan_snapshot_component WHERE plan_id = ?",
                    (plan_id,),
                ).fetchall()
                components = _components_from_stored_digests(tier, rows)
                if plan_identifier.compute_v2(components) != plan_id:
                    raise PrivateSnapshotIntegrityError("plan_id")
                return plan_id, components
            finally:
                if conn.in_transaction:
                    conn.rollback()
    except (PrivateSnapshotError, plan_identifier.PlanV2ComponentError):
        raise
    except sqlite3.Error:
        raise PrivateSnapshotStoreError() from None


def private_snapshot_for_run(
    run_id: str, components: plan_identifier.PlanV2Components
) -> VerifiedPrivateSnapshot:
    try:
        with closing(_connect_readonly()) as conn:
            if not _table_exists(conn, "workflow_run_plan_snapshot"):
                raise PrivateSnapshotIntegrityError("run reference", "is missing")
            row = conn.execute(
                "SELECT plan_id FROM workflow_run_plan_snapshot WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise PrivateSnapshotIntegrityError("run reference", "is missing")
            return _load_verified(conn, row[0], components)
    except (PrivateSnapshotError, plan_identifier.PlanV2ComponentError):
        raise
    except sqlite3.Error:
        raise PrivateSnapshotStoreError() from None


def release_run_reference(run_id: str, conn: Optional[sqlite3.Connection] = None) -> None:
    """Release one run and garbage-collect bytes only after the last reference.

    SQLite foreign keys are normally disabled in this repository. The declared
    constraints document relationships, while this manual last-reference
    protocol provides the active lifecycle safety. Removing rows does not claim
    secure erasure from database pages or filesystem history.
    """

    def _release(active: sqlite3.Connection) -> None:
        row = active.execute(
            "SELECT plan_id FROM workflow_run_plan_snapshot WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return
        if not _tighten_permissions(_connection_path(active)):
            logger.warning("private snapshot store not owner-only; releasing raw bytes anyway")
        plan_id = row[0]
        active.execute("DELETE FROM workflow_run_plan_snapshot WHERE run_id = ?", (run_id,))
        remaining = active.execute(
            "SELECT 1 FROM workflow_run_plan_snapshot WHERE plan_id = ? LIMIT 1",
            (plan_id,),
        ).fetchone()
        if remaining is None:
            active.execute(
                "DELETE FROM workflow_plan_snapshot_component WHERE plan_id = ?",
                (plan_id,),
            )
            active.execute("DELETE FROM workflow_plan_snapshot WHERE plan_id = ?", (plan_id,))

    if conn is not None:
        try:
            _release(conn)
        except PrivateSnapshotError:
            raise
        except sqlite3.Error:
            raise PrivateSnapshotStoreError() from None
        return
    from cli_agent_orchestrator.constants import DATABASE_FILE

    if not Path(DATABASE_FILE).exists():
        return
    try:
        with closing(_connect()) as own:
            with own:
                _release(own)
    except PrivateSnapshotError:
        raise
    except sqlite3.Error:
        raise PrivateSnapshotStoreError() from None
