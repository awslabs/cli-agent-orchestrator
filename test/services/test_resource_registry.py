"""Tests for the code-owned resource registry (T-RES-REG fork side)."""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid

import pytest

from cli_agent_orchestrator.services.resource_registry import (
    RegistryConflict,
    RegistryError,
    RegistryNotFound,
    RegistryTransitionRefused,
    ResourceRegistry,
)


@pytest.fixture
def registry(tmp_path):
    return ResourceRegistry(tmp_path / "recovery" / "resource-registry-fork.db")


def _declare(registry, entry_id, **changes):
    kwargs = {
        "entry_id": entry_id,
        "kind": "socket",
        "protocol_vintage": "v2",
        "terminal_id": "a1b2c3d4",
        "generation": "gen-000042",
        "owner": "fork",
        "ownership": "owned",
        "constructor_id": "managed-launch-v2",
        "deleter_id": "managed-launch-v2",
        "rollback_rule": "generation-isolated",
        "desired_fs_path": f"/state/{entry_id}/bridge.sock",
        "actor_id": "test",
    }
    kwargs.update(changes)
    return registry.declare(**kwargs)


def test_creation_meta_and_permissions(registry, tmp_path):
    path = tmp_path / "recovery" / "resource-registry-fork.db"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    conn = sqlite3.connect(str(path))
    rows = dict(conn.execute("SELECT k, v FROM registry_meta"))
    conn.close()
    assert rows["registry_schema_version"] == "1"
    assert rows["db_uuid"]
    assert rows["created_at"]


def test_absent_meta_refused(tmp_path):
    path = tmp_path / "r.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        "CREATE TABLE registry_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID;"
    )
    conn.commit()
    conn.close()
    with pytest.raises(RegistryError, match="meta"):
        ResourceRegistry(path)


def test_declared_before_physical_and_journal_first(registry):
    entry = _declare(registry, "e-" + uuid.uuid4().hex[:8])
    assert entry["lifecycle_state"] == "declared"
    assert entry["state_seq"] == 1
    assert entry["events"][0]["to_state"] == "declared"


def test_owned_desired_identity_must_embed_entry_id(registry):
    entry_id = "e-abc123"
    with pytest.raises(RegistryError, match="embed"):
        _declare(registry, entry_id, desired_fs_path="/state/other/bridge.sock")
    entry = _declare(registry, entry_id)
    assert entry_id in entry["desired_fs_path"]


def test_external_entries_record_verbatim_identity(registry):
    entry = _declare(
        registry,
        "e-ext",
        ownership="external",
        desired_tmux_name="cao-shared-server",  # no entry_id embedding
        kind="tmux_server_state",
        monitor_id="watchdog-1",
    )
    assert entry["desired_tmux_name"] == "cao-shared-server"


def test_full_lifecycle_with_receipts(registry):
    entry_id = "e-" + uuid.uuid4().hex[:8]
    _declare(registry, entry_id)
    created = registry.register_created(
        entry_id,
        actor_id="test",
        observed={"observed_pid": 4242},
        existence_receipt_digest="1" * 64,
    )
    assert created["lifecycle_state"] == "created"
    assert created["observed_pid"] == 4242
    active = registry.activate(entry_id, actor_id="test", existence_receipt_digest="2" * 64)
    assert active["lifecycle_state"] == "active"
    registry.drain(entry_id, actor_id="test")
    registry.close(entry_id, actor_id="test")
    deleted = registry.delete(entry_id, actor_id="test", verified_absence_digest="3" * 64)
    assert deleted["lifecycle_state"] == "deleted"
    states = [event["to_state"] for event in deleted["events"]]
    assert states == ["declared", "created", "active", "draining", "closed", "deleted"]


def test_illegal_transitions_refused(registry):
    entry_id = "e-illegal"
    _declare(registry, entry_id)
    with pytest.raises(RegistryTransitionRefused):
        registry.activate(entry_id, actor_id="test", existence_receipt_digest="1" * 64)
    with pytest.raises(RegistryTransitionRefused):
        registry.delete(entry_id, actor_id="test")
    with pytest.raises(RegistryNotFound):
        registry.resolve("e-missing")


def test_activation_requires_active_dependencies(registry):
    parent = "e-parent"
    child = "e-child"
    _declare(registry, parent)
    _declare(registry, child, depends_on=(parent,))
    registry.register_created(child, actor_id="t", existence_receipt_digest="1" * 64)
    with pytest.raises(RegistryTransitionRefused, match="dependency"):
        registry.activate(child, actor_id="t", existence_receipt_digest="2" * 64)
    registry.register_created(parent, actor_id="t", existence_receipt_digest="3" * 64)
    registry.activate(parent, actor_id="t", existence_receipt_digest="4" * 64)
    registry.activate(child, actor_id="t", existence_receipt_digest="2" * 64)


def test_dependency_cycle_refused_by_trigger(registry):
    _declare(registry, "e-a")
    _declare(registry, "e-b", depends_on=("e-a",))
    # e-a -> e-b would close a cycle (e-b already depends on e-a); the
    # trigger's RAISE(ABORT) surfaces as an IntegrityError.
    conn = registry._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO resource_dependency(entry_id, depends_on_entry_id) "
                "VALUES ('e-a','e-b')"
            )
            conn.commit()
    finally:
        conn.close()


def test_state_seq_cas_enforced_by_trigger(registry):
    entry_id = "e-cas"
    _declare(registry, entry_id)
    conn = registry._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE resource SET state_seq=5 WHERE entry_id=?", (entry_id,))
    finally:
        conn.close()


def test_terminal_rows_immutable_and_never_deleted(registry):
    entry_id = "e-term"
    _declare(registry, entry_id)
    registry.register_created(entry_id, actor_id="t", existence_receipt_digest="1" * 64)
    registry.drain(entry_id, actor_id="t")
    registry.close(entry_id, actor_id="t")
    registry.delete(entry_id, actor_id="t")
    conn = registry._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM resource WHERE entry_id=?", (entry_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE resource SET state_seq=99 WHERE entry_id=?", (entry_id,))
    finally:
        conn.close()


def test_abort_requires_verified_absence(registry):
    entry_id = "e-abort"
    _declare(registry, entry_id)
    with pytest.raises(RegistryError, match="verified-absence"):
        registry.abort(entry_id, actor_id="t", verified_absence_digest="short")
    aborted = registry.abort(entry_id, actor_id="t", verified_absence_digest="0" * 64)
    assert aborted["lifecycle_state"] == "aborted"


def test_discover_records_observed_identity_after_crash_window(registry):
    # Crash injected between physical creation and observed-ID capture:
    # the entry is still `declared`, and the restart reconciliation finds
    # the resource by its embedded entry_id and records the observed id.
    entry_id = "e-" + uuid.uuid4().hex[:8]
    _declare(registry, entry_id, kind="tmux_window", desired_tmux_name=f"managed-{entry_id}")
    found = registry.discover(
        entry_id,
        actor_id="reconciler",
        finder=lambda entry: (
            {"observed_tmux_id": "@42"} if entry_id in entry["desired_tmux_name"] else None
        ),
    )
    assert found["lifecycle_state"] == "created"
    assert found["observed_tmux_id"] == "@42"


def test_discover_no_trace_leaves_declared(registry):
    entry_id = "e-notrace"
    _declare(registry, entry_id)
    entry = registry.discover(entry_id, actor_id="reconciler", finder=lambda e: None)
    assert entry["lifecycle_state"] == "declared"


def test_live_identity_uniqueness_and_terminal_freeing(registry):
    # The colliding path embeds both entry ids (owned identities must
    # embed their own entry_id), so the partial-unique index is what
    # actually refuses the live duplicate.
    shared_path = "/state/collide-e-one-e-two"
    _declare(registry, "e-one", desired_fs_path=shared_path)
    with pytest.raises(RegistryConflict):
        _declare(registry, "e-two", desired_fs_path=shared_path)
    # Once the first entry reaches a terminal state the identity frees.
    registry.register_created("e-one", actor_id="t", existence_receipt_digest="1" * 64)
    registry.drain("e-one", actor_id="t")
    registry.close("e-one", actor_id="t")
    registry.delete("e-one", actor_id="t")
    _declare(registry, "e-two", desired_fs_path=shared_path)


def test_idempotent_transition_redrive(registry):
    entry_id = "e-redrive"
    _declare(registry, entry_id)
    first = registry.register_created(entry_id, actor_id="t", existence_receipt_digest="1" * 64)
    second = registry.register_created(entry_id, actor_id="t", existence_receipt_digest="1" * 64)
    assert first["lifecycle_state"] == second["lifecycle_state"] == "created"
    events = [e["to_state"] for e in second["events"]]
    assert events.count("created") == 1


def test_drain_order_reverse_dependency(registry):
    _declare(registry, "e-base")
    _declare(registry, "e-mid", depends_on=("e-base",))
    _declare(registry, "e-top", depends_on=("e-mid",))
    for entry_id in ("e-base", "e-mid", "e-top"):
        registry.register_created(entry_id, actor_id="t", existence_receipt_digest="1" * 64)
    registry.activate("e-base", actor_id="t", existence_receipt_digest="2" * 64)
    registry.activate("e-mid", actor_id="t", existence_receipt_digest="2" * 64)
    registry.activate("e-top", actor_id="t", existence_receipt_digest="2" * 64)
    order = [
        entry["entry_id"]
        for entry in registry.drain_order(terminal_id="a1b2c3d4", generation="gen-000042")
    ]
    assert order.index("e-top") < order.index("e-mid") < order.index("e-base")


def test_monitor_only_for_external_shared(registry):
    owned = _declare(registry, "e-owned-mon")
    with pytest.raises(RegistryTransitionRefused):
        registry.monitor("e-owned-mon", actor_id="t", monitor_id="m")
    _declare(
        registry,
        "e-ext-mon",
        ownership="shared",
        kind="tmux_server_state",
        desired_tmux_name="cao-server",
    )
    entry = registry.monitor("e-ext-mon", actor_id="t", monitor_id="watchdog-2")
    assert entry["monitor_id"] == "watchdog-2"


def test_insert_or_replace_hostile_rewrite_refused(registry):
    # With recursive_triggers=ON the REPLACE conflict-delete fires the
    # append-only triggers; the rewrite is refused rather than silently
    # rewriting history.
    _declare(registry, "e-hostile")
    conn = registry._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO resource(entry_id, kind, protocol_vintage, "
                "terminal_id, generation, owner, ownership, constructor_id, "
                "deleter_id, lifecycle_state, state_seq, rollback_rule, "
                "desired_fs_path) VALUES ('e-hostile','socket','v2','a1b2c3d4',"
                "'gen-000042','fork','owned','x','x','active',1,"
                "'generation-isolated','/tmp/e-hostile')"
            )
    finally:
        conn.close()
    assert registry.resolve("e-hostile")["lifecycle_state"] == "declared"


def test_enumerate_filters(registry):
    _declare(registry, "e-f1", generation="gen-A")
    _declare(registry, "e-f2", generation="gen-B")
    assert len(registry.enumerate(generation="gen-A")) == 1
    assert len(registry.enumerate(terminal_id="a1b2c3d4")) == 2
    assert registry.enumerate(generation="gen-A")[0]["entry_id"] == "e-f1"
