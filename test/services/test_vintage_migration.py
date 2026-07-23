"""Tests for the transactional v2 vintage migration/rollback/drain (T-MIG)."""

from __future__ import annotations

import sqlite3

import pytest

from cli_agent_orchestrator.services import vintage_migration as vm


def _count(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _seed_v1(db_path):
    """A pre-existing v1 table the migration must never touch."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS terminals (id TEXT PRIMARY KEY, note TEXT)")
        conn.execute("INSERT OR REPLACE INTO terminals(id, note) VALUES ('v1-row', 'keep')")
        conn.commit()
    finally:
        conn.close()


def test_migrate_creates_surface_and_is_idempotent(tmp_path):
    db_path = tmp_path / "metadata.db"
    _seed_v1(db_path)
    receipt = vm.migrate_v2(db_path)
    assert receipt["action"] == "migrate"
    assert receipt["already_present"] is False
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"managed_launch_v2_reservations", "managed_launch_v2_terminals"} <= tables
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(managed_launch_v2_reservations)")
        }
        assert "bind_intent_json" in columns
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='managed_launch_v2_reservations'"
        ).fetchone()[0]
        assert "CHECK (protocol_vintage = 'v2')" in ddl
    finally:
        conn.close()
    again = vm.migrate_v2(db_path)
    assert again["already_present"] is True
    # The migration journal is append-only: both runs are recorded.
    assert _count(db_path, vm.JOURNAL_TABLE) == 2
    # Pre-existing v1 rows were never touched.
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT note FROM terminals WHERE id='v1-row'").fetchone()[0] == "keep"
    finally:
        conn.close()


def test_migrate_backfills_bind_intent_on_pre_existing_surface(tmp_path):
    db_path = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # The pre-correction shape: no bind_intent_json column.
        conn.execute(
            "CREATE TABLE managed_launch_v2_reservations ("
            "reservation_id TEXT PRIMARY KEY, terminal_id TEXT NOT NULL UNIQUE, "
            "generation TEXT NOT NULL UNIQUE, protocol_vintage TEXT NOT NULL DEFAULT 'v2' "
            "CHECK (protocol_vintage = 'v2'), session_name TEXT NOT NULL, "
            "provider TEXT NOT NULL, agent_profile TEXT NOT NULL, caller_id TEXT NOT NULL, "
            "working_directory TEXT NOT NULL, trusted_project_root TEXT, "
            "obligation_generation TEXT NOT NULL, task_id TEXT, run_id TEXT NOT NULL, "
            "launch_nonce_digest TEXT NOT NULL, state TEXT NOT NULL, request_json TEXT NOT NULL, "
            "binding_json TEXT, admission_json TEXT, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    vm.migrate_v2(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(managed_launch_v2_reservations)")
        }
        assert "bind_intent_json" in columns
    finally:
        conn.close()


def _insert_v2_reservation(db_path, reservation_id="r-1", generation="gen-1"):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO managed_launch_v2_reservations ("
            "reservation_id, terminal_id, generation, protocol_vintage, session_name, "
            "provider, agent_profile, caller_id, working_directory, trusted_project_root, "
            "obligation_generation, task_id, run_id, launch_nonce_digest, state, "
            "request_json, binding_json, admission_json, created_at, updated_at) VALUES ("
            "?, 't-1', ?, 'v2', 's', 'codex', 'p', 'c', '/w', '/w', 'ob', 'task', 'run', "
            "'d', 'reserved', '{}', NULL, NULL, 'now', 'now')",
            (reservation_id, generation),
        )
        conn.commit()
    finally:
        conn.close()


def test_rollback_refused_until_complete_drain(tmp_path):
    db_path = tmp_path / "metadata.db"
    _seed_v1(db_path)
    vm.migrate_v2(db_path)
    _insert_v2_reservation(db_path)
    report = vm.drain_report(db_path)
    assert report["drained"] is False
    assert report["tables"]["managed_launch_v2_reservations"] == 1
    # A rollback that would strand a live v2 generation refuses with zero mutation.
    with pytest.raises(vm.RollbackRefused):
        vm.rollback_v2(db_path)
    assert _count(db_path, "managed_launch_v2_reservations") == 1
    # After a complete drain the rollback succeeds transactionally.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM managed_launch_v2_reservations")
        conn.commit()
    finally:
        conn.close()
    assert vm.drain_report(db_path)["drained"] is True
    receipt = vm.rollback_v2(db_path)
    assert receipt["action"] == "rollback"
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "managed_launch_v2_reservations" not in tables
        assert "managed_launch_v2_terminals" not in tables
        # v1 rows and the append-only journal survive the rollback.
        assert conn.execute("SELECT note FROM terminals WHERE id='v1-row'").fetchone()[0] == "keep"
        actions = [
            row[0] for row in conn.execute(f"SELECT action FROM {vm.JOURNAL_TABLE} ORDER BY at")
        ]
        assert "rollback" in actions
    finally:
        conn.close()


def test_wal_resident_rows_are_visible_to_the_drain_decision(tmp_path):
    # WAL preservation: rows committed but still WAL-resident (no
    # checkpoint) must be visible to the drain decision — rollback never
    # destroys committed state it failed to see.
    db_path = tmp_path / "metadata.db"
    vm.migrate_v2(db_path)
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO managed_launch_v2_reservations ("
            "reservation_id, terminal_id, generation, protocol_vintage, session_name, "
            "provider, agent_profile, caller_id, working_directory, trusted_project_root, "
            "obligation_generation, task_id, run_id, launch_nonce_digest, state, "
            "request_json, binding_json, admission_json, created_at, updated_at) VALUES ("
            "'r-wal', 't-wal', 'gen-wal', 'v2', 's', 'codex', 'p', 'c', '/w', '/w', 'ob', "
            "'task', 'run', 'd', 'reserved', '{}', NULL, NULL, 'now', 'now')"
        )
        writer.commit()
        # Deliberately no checkpoint: the row may be WAL-resident only.
        assert (tmp_path / "metadata.db-wal").exists()
        report = vm.drain_report(db_path)
        assert report["drained"] is False
        with pytest.raises(vm.RollbackRefused):
            vm.rollback_v2(db_path)
    finally:
        writer.close()
