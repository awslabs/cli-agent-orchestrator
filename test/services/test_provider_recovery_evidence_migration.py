"""Fresh and upgraded stores agree on the M6a episode journal schema."""

import sqlite3

from sqlalchemy import create_engine, inspect

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients import database

EXPECTED_INDEXES = {
    "ix_provider_recovery_episode_active_generation": True,
    "ix_provider_recovery_episode_generation_history": False,
}


def test_create_all_installs_episode_indexes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        database.Base.metadata.create_all(bind=engine)
        indexes = {
            row["name"]: bool(row["unique"])
            for row in inspect(engine).get_indexes("provider_recovery_episodes")
        }
        assert indexes == EXPECTED_INDEXES
    finally:
        engine.dispose()


def test_upgrade_migration_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "upgraded.db"
    path.touch()
    monkeypatch.setattr(constants, "DATABASE_FILE", path)

    database._migrate_provider_recovery_episodes()
    database._migrate_provider_recovery_episodes()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_recovery_episodes)")}
        indexes = {
            row[1]: bool(row[2])
            for row in conn.execute("PRAGMA index_list(provider_recovery_episodes)")
        }
    assert {
        "occurrence_id",
        "terminal_id",
        "generation_key",
        "generation",
        "provider",
        "pattern",
        "fingerprint",
        "match_json",
        "active",
        "opened_at",
        "last_observed_at",
        "closed_at",
    } == columns
    assert {name: indexes[name] for name in EXPECTED_INDEXES} == EXPECTED_INDEXES
