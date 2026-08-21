"""Tests for the durable plan-approval record (issue #583 Bolt 2, unit ``approval-store``).

Two carry the unit's load:

* ``test_a_repeated_grant_does_not_rewrite_the_original`` — write-once. If this fails, an existing approval can
  be pointed at a changed plan: the row reads as approved, the timestamp looks recent, and work nobody reviewed
  executes with a genuine approval behind it.
* ``test_an_unknown_plan_is_not_approved`` — absence means unapproved. Every fault in this unit (missing table,
  silent migration failure, database error) converges on "no row found", so if absence were ever permissive each
  one would become an authorisation bypass rather than a refused run.
"""

import importlib
import sqlite3

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients import database as database_client
from cli_agent_orchestrator.services import approval_store


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Repoint ``DATABASE_FILE`` at a temporary database, WITHOUT running ``init_db``.

    This is the condition that made migrator-on-connect necessary rather than optional: the journal's own
    ``_MIGRATED_PATHS`` comment records that five test modules do exactly this, so a unit relying on the FastAPI
    lifespan would find no table here.
    """
    path = tmp_path / "cao.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(database_client, "DATABASE_FILE", path, raising=False)
    importlib.reload(approval_store)
    monkeypatch.setattr(approval_store, "DATABASE_FILE", path, raising=False)
    return path


def _columns(path):
    with sqlite3.connect(str(path)) as conn:
        return {row[1]: row for row in conn.execute("PRAGMA table_info(workflow_plan_approval)")}


# ---------------------------------------------------------------------------
# The two load-bearing properties
# ---------------------------------------------------------------------------


def test_a_repeated_grant_does_not_rewrite_the_original(db_path):
    """WRITE-ONCE. ``INSERT OR IGNORE`` makes it a property of the statement, not of a caller's check."""
    approval_store.grant("plan-v1:abc", "stan")
    first = approval_store.get_approval("plan-v1:abc")
    assert first is not None

    approval_store.grant("plan-v1:abc", "someone-else")
    second = approval_store.get_approval("plan-v1:abc")

    assert second is not None
    assert second.approved_by == "stan", "a second grant must not change the approver"
    assert second.approved_at == first.approved_at, "a second grant must not change the timestamp"


def test_an_unknown_plan_is_not_approved(db_path):
    """Absence answers False and raises nothing — so every fault mode is fail-closed."""
    assert approval_store.is_approved("plan-v1:never-granted") is False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_the_table_exists_on_a_fresh_database_with_its_three_columns(db_path):
    """The unit's whole mitigation for a SILENT migration failure.

    ``_migrate_workflow_plan_approval`` swallows failure at debug level, so its return says nothing about the
    schema. A missing table makes every lookup answer False, refusing every run — diagnosed far from its cause.
    """
    approval_store.is_approved("anything")  # forces a connect, which runs the migrator

    cols = _columns(db_path)
    assert set(cols) == {"plan_id", "approved_at", "approved_by"}
    assert (
        cols["plan_id"][5] == 1
    ), "plan_id must be the PRIMARY KEY — one approval per plan, enforced by the DB"
    assert cols["approved_at"][3] == 1, "approved_at is NOT NULL"
    assert cols["approved_by"][3] == 1, "approved_by is NOT NULL"


def test_the_migration_is_idempotent(db_path):
    database_client._migrate_workflow_plan_approval()
    database_client._migrate_workflow_plan_approval()
    assert set(_columns(db_path)) == {"plan_id", "approved_at", "approved_by"}


def test_it_works_against_a_repointed_database_without_init_db(db_path):
    """The condition that made migrator-on-connect necessary. ``init_db`` is never called in this module."""
    assert not db_path.exists() or "workflow_plan_approval" not in str(_columns(db_path))
    approval_store.grant("plan-v1:fresh", "stan")
    assert approval_store.is_approved("plan-v1:fresh") is True


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_grant_then_is_approved(db_path):
    assert approval_store.is_approved("plan-v1:x") is False
    approval_store.grant("plan-v1:x", "stan")
    assert approval_store.is_approved("plan-v1:x") is True


def test_one_grant_does_not_approve_a_different_plan(db_path):
    """The whole re-approval mechanism: a changed plan is a different plan_id and finds no row."""
    approval_store.grant("plan-v1:aaa", "stan")
    assert approval_store.is_approved("plan-v1:bbb") is False


def test_plan_id_is_stored_verbatim_and_never_normalised(db_path):
    """A normalisation is how two distinct plans could come to share one approval."""
    for value in ("plan-v1:abc", "v2:legacy-looking", "no-scheme-at-all", "  spaced  "):
        approval_store.grant(value, "stan")
        record = approval_store.get_approval(value)
        assert record is not None
        assert record.plan_id == value


def test_get_approval_returns_the_record_and_none_when_absent(db_path):
    assert approval_store.get_approval("plan-v1:missing") is None
    approval_store.grant("plan-v1:present", "stan")
    record = approval_store.get_approval("plan-v1:present")
    assert record is not None
    assert record.approved_by == "stan"
    assert record.approved_at.endswith(
        "+00:00"
    ), "timestamps are UTC ISO, matching workflow_run.started_at"


# ---------------------------------------------------------------------------
# The surface, whose ABSENCES are the control
# ---------------------------------------------------------------------------


def test_the_module_offers_no_update_delete_or_replacing_upsert():
    """The absence IS the security control — an update path could transfer an approval to a changed plan.

    Asserted on the public surface rather than by behaviour, because the property is the absence of a
    capability. Same shape as pass 2A's leaf-module posture tests.
    """
    public = {n for n in dir(approval_store) if not n.startswith("_")}
    for forbidden in (
        "update",
        "delete",
        "revoke",
        "upsert",
        "replace",
        "set_approval",
        "update_approval",
    ):
        assert forbidden not in public, f"{forbidden} must not exist on this module"

    source = open(approval_store.__file__, encoding="utf-8").read()
    code = "".join(source.replace('"""', "\x00").split("\x00")[::2])
    assert "UPDATE workflow_plan_approval" not in code
    assert "DELETE FROM workflow_plan_approval" not in code
    assert "INSERT OR REPLACE" not in code
    assert "ON CONFLICT" not in code, "an upsert that replaces would defeat write-once"
