"""#1199: eight nullable record columns for the local match, an additive migration, the store."""

import json
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BRIEF = "inspect the synthetic logs"
COLUMNS = (
    "match_rule",
    "match_needs",
    "match_covered",
    "match_tiebreak",
    "match_from",
    "match_pick",
    "catalog_version",
    "catalog_hash",
)
CATALOG = '{"ignore_features":[],"long_context_tokens":500000,"models":{},"version":1}'
BASE = dict(
    point="provider.route",
    state="on",
    kind="assign",
    provider="claude_code",
    fallback_source="creator",
    fallback_value="claude_code",
    outcome="applied",
)


@pytest.fixture
def store(tmp_path):
    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.store import DecisionStore

    db = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    DecisionRecordModel.__table__.create(db)
    events = []
    value = DecisionStore(sessionmaker(bind=db), tmp_path / "decision-hash.key", emit=events.append)
    value.events = events
    return value


def test_the_columns_round_trip_and_lists_come_back_as_lists(store):
    record_id = store.insert(
        {
            **BASE,
            "match_rule": "covered",
            "match_needs": ("long_context", "read_only"),
            "match_covered": ["long_context"],
            "match_tiebreak": "only",
            "match_pick": "claude_code/large",
            "catalog_version": 1,
            "catalog_canonical": CATALOG,
        },
        BRIEF,
    )
    row = store.get(record_id)
    assert row["match_needs"] == ["long_context", "read_only"]
    assert row["match_covered"] == ["long_context"]
    assert (row["match_rule"], row["match_tiebreak"], row["match_pick"]) == (
        "covered",
        "only",
        "claude_code/large",
    )
    assert row["catalog_version"] == 1
    other = store.get(store.insert({**BASE, "point": "model.route"}, BRIEF))
    assert all(other[column] is None for column in COLUMNS)


def test_the_catalog_hash_is_the_keyed_hmac_and_the_text_is_never_stored(store):
    from cli_agent_orchestrator.decisions import catalog
    from cli_agent_orchestrator.decisions.catalog import load_catalog

    record_id = store.insert({**BASE, "catalog_canonical": CATALOG}, BRIEF)
    row = store.get(record_id)
    loaded = load_catalog({"decisions": {"match": {"version": 1}}})
    assert loaded.canonical() == CATALOG
    key = catalog.read_hash_key(store.key_path)
    assert (row["catalog_hash"], row["hash_key_id"]) == catalog.catalog_hash(loaded, key)
    assert "catalog_canonical" not in row
    assert CATALOG not in json.dumps(row)


def test_a_catalog_hash_without_a_message_still_names_its_key(store):
    row = store.get(store.insert({**BASE, "catalog_canonical": CATALOG}, None))
    assert row["catalog_hash"] and row["hash_key_id"] and row["message_hash"] is None


def test_update_match_writes_only_rows_without_a_match(store):
    first = store.insert({**BASE}, BRIEF)
    second = store.insert({**BASE, "match_rule": "no_features"}, BRIEF)
    store.update_match(
        (first, second),
        {
            "match_rule": "covered",
            "match_needs": ["read_only"],
            "match_covered": ["read_only"],
            "match_tiebreak": "only",
            "match_pick": "codex/medium",
            "catalog_version": 1,
            "catalog_canonical": CATALOG,
        },
    )
    updated, kept = store.get(first), store.get(second)
    assert (updated["match_rule"], updated["match_needs"], updated["match_pick"]) == (
        "covered",
        ["read_only"],
        "codex/medium",
    )
    assert updated["catalog_hash"] is not None
    # Written once: a row that already has a match keeps it.
    assert kept["match_rule"] == "no_features" and kept["match_pick"] is None
    # The decision and launch columns are untouched.
    assert (updated["outcome"], updated["answer"], updated["applied_value"]) == (
        "applied",
        None,
        None,
    )
    store.update_match((first,), {"match_rule": "no_cover"})
    assert store.get(first)["match_rule"] == "covered"
    # Records only: nothing was emitted.
    assert store.events == []


MATCH = {
    "match_rule": "covered",
    "match_needs": ["read_only"],
    "match_covered": ["read_only"],
    "match_tiebreak": "only",
    "match_pick": "codex/medium",
    "catalog_version": 1,
    "catalog_canonical": CATALOG,
}


def test_update_match_names_the_key_its_catalog_hash_is_under(store):
    from cli_agent_orchestrator.decisions import catalog
    from cli_agent_orchestrator.decisions.catalog import load_catalog
    from cli_agent_orchestrator.decisions.hashing import rotate_key

    loaded = load_catalog({"decisions": {"match": {"version": 1}}})
    # A row hashed with no message has no key id yet: the update gives it the hash's.
    bare = store.insert({**BASE}, None)
    assert store.get(bare)["hash_key_id"] is None
    store.update_match((bare,), MATCH)
    row = store.get(bare)
    key = catalog.read_hash_key(store.key_path)
    assert (row["catalog_hash"], row["hash_key_id"]) == catalog.catalog_hash(loaded, key)
    # A row inserted under the old key, then the key is rotated before the shadow update.
    old = store.insert({**BASE}, BRIEF)
    before = store.get(old)
    rotate_key(store.key_path)
    fresh = store.insert({**BASE}, BRIEF)
    store.update_match((old, fresh), MATCH)
    kept, matched = store.get(old), store.get(fresh)
    # The old row keeps its key id and message hash, and gets no hash under another key.
    assert (kept["hash_key_id"], kept["message_hash"]) == (
        before["hash_key_id"],
        before["message_hash"],
    )
    assert kept["match_rule"] is None and kept["catalog_hash"] is None
    # The row under the current key gets the match, its hash under the key it names.
    new_key = catalog.read_hash_key(store.key_path)
    assert new_key != key
    assert matched["match_rule"] == "covered"
    assert (matched["catalog_hash"], matched["hash_key_id"]) == catalog.catalog_hash(
        loaded, new_key
    )
    # An invalid catalog has no hash, so the update needs no key and writes any unmatched row.
    store.update_match((old,), {"match_rule": "catalog_invalid", "catalog_version": None})
    assert store.get(old)["match_rule"] == "catalog_invalid"
    assert store.get(old)["hash_key_id"] == before["hash_key_id"]


def test_update_match_refuses_any_other_column(store, caplog):
    record_id = store.insert({**BASE}, BRIEF)
    store.update_match((record_id,), {"match_rule": "covered", "outcome": "fallback"})
    row = store.get(record_id)
    assert (row["match_rule"], row["outcome"]) == (None, "applied")
    assert "Decision match update failed" in caplog.text


def test_telemetry_carries_the_match_rule_label():
    from cli_agent_orchestrator.decisions import telemetry

    assert telemetry.FIELDS["match_rule"] == "match_rule"
    # Only the rule: no needs, pick or hash reach telemetry.
    assert not {"match_needs", "match_covered", "match_pick", "catalog_hash"} & set(
        telemetry.FIELDS
    )


def _legacy_decision_table(path):
    rows = [(1, "model.route", "fixture", "error"), (2, "provider.route", None, "unsure")]
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE decision_records (id INTEGER PRIMARY KEY, point TEXT, decider TEXT, "
            "reason TEXT)"
        )
        db.executemany("INSERT INTO decision_records VALUES (?,?,?,?)", rows)
    return rows


def test_the_migration_is_additive_nullable_idempotent_and_leaves_rows(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients import database

    path = tmp_path / "legacy.db"
    rows = _legacy_decision_table(path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_match()
    database._migrate_decision_records_match()
    with sqlite3.connect(path) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(decision_records)")}
        migrated = db.execute(
            "SELECT id, point, decider, reason, " + ", ".join(COLUMNS) + " FROM decision_records"
        ).fetchall()
    for column in COLUMNS:
        assert columns[column][3] == 0 and columns[column][4] is None  # nullable, no default
    assert columns["catalog_version"][2] == "INTEGER"
    assert len(columns) == 4 + len(COLUMNS)
    assert migrated == [row + (None,) * len(COLUMNS) for row in rows]


def test_the_migration_adds_only_the_missing_columns(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients import database

    path = tmp_path / "partial.db"
    _legacy_decision_table(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE decision_records ADD COLUMN match_rule TEXT")
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_match()
    with sqlite3.connect(path) as db:
        names = [row[1] for row in db.execute("PRAGMA table_info(decision_records)")]
    assert all(names.count(column) == 1 for column in COLUMNS)


def test_the_migration_creates_nothing_without_the_table(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients import database

    path = tmp_path / "other.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    database._migrate_decision_records_match()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_init_db_adds_the_columns_to_an_existing_database(tmp_path, monkeypatch):
    from unittest.mock import patch

    from cli_agent_orchestrator.clients import database

    path = tmp_path / "legacy.db"
    _legacy_decision_table(path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    with patch.object(database, "Base"):
        database.init_db()
    with sqlite3.connect(path) as db:
        names = {row[1] for row in db.execute("PRAGMA table_info(decision_records)")}
    assert set(COLUMNS) <= names


def test_a_fresh_database_has_the_columns():
    from cli_agent_orchestrator.clients.database import DecisionRecordModel

    columns = DecisionRecordModel.__table__.columns
    assert all(columns[column].nullable for column in COLUMNS)
