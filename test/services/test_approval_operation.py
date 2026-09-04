"""Tests for granting an approval (issue #583 Bolt 2, unit ``approval-operation``).

Three carry the unit's load:

* ``test_a_repeat_grant_reports_the_original_approver_and_timestamp`` — this unit's worst failure mode is a
  command that APPEARS to work. ``grant`` is ``INSERT OR IGNORE``, so a plain "success" would leave an operator
  unable to tell "I just approved this" from "someone approved it last week and my grant did nothing".
* ``test_the_approve_endpoint_requires_the_admin_scope`` — removing the MCP grant tool is not sufficient on its
  own, because the MCP boundary reaches the backplane over HTTP. If this endpoint accepted ``cao:write`` a
  write-scoped agent token could grant directly. Asserted against the dependency so "aligning" it with the
  sibling ``WRITE, ADMIN`` endpoints fails here rather than silently widening the grant.
* ``test_no_mcp_tool_can_grant_an_approval`` — an absence, and absences do not fail on their own.
"""

import json

import pytest

from cli_agent_orchestrator.services import approval_provenance

# ---------------------------------------------------------------------------
# approved_by — the bounding that discharges SR-2B1-7
# ---------------------------------------------------------------------------


def test_a_long_account_name_is_truncated():
    """The column is unbounded TEXT and the row can never be updated or deleted."""
    bounded = approval_provenance.bound("a" * 500)
    assert len(bounded) == approval_provenance.APPROVED_BY_MAX_LENGTH


def test_control_characters_are_stripped():
    """A newline in ``USER`` would split one log line into two apparent events.

    This unit logs ``approved_by`` at info as the only record of when an approval intent was
    expressed, so a value that can forge a second log entry is a real problem rather than a tidy one.
    """
    bounded = approval_provenance.bound("stan\nGRANTED: plan-v1:evil by root")
    assert "\n" not in bounded
    assert "\r" not in bounded


def test_an_empty_or_whitespace_account_becomes_the_placeholder():
    for empty in ("", "   ", "\n\t"):
        assert approval_provenance.bound(empty) == approval_provenance.UNRESOLVED_ACCOUNT


def test_local_account_never_raises(monkeypatch):
    """A grant must not fail because a provenance NOTE could not be written."""

    def _boom():
        raise OSError("no passwd entry, no environment")

    monkeypatch.setattr(approval_provenance.getpass, "getuser", _boom)
    assert approval_provenance.local_account() == approval_provenance.UNRESOLVED_ACCOUNT


def test_local_account_bounds_a_hostile_environment_value(monkeypatch):
    """``getpass.getuser`` reads LOGNAME/USER/LNAME/USERNAME — environment, therefore INPUT.

    Its air of being an ambient fact about the machine is exactly what would get it written unchecked.
    """
    monkeypatch.setattr(approval_provenance.getpass, "getuser", lambda: "x" * 200 + "\n")
    resolved = approval_provenance.local_account()
    assert len(resolved) == approval_provenance.APPROVED_BY_MAX_LENGTH
    assert "\n" not in resolved


def test_the_placeholder_is_not_mistakable_for_a_username():
    """A reader comparing rows must be able to tell "unknown" from "someone called unknown"."""
    assert "-" in approval_provenance.UNRESOLVED_ACCOUNT
    assert approval_provenance.UNRESOLVED_ACCOUNT != "unknown"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient against a temporary database, with the approval table migrated."""
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as database_client
    from cli_agent_orchestrator.services import approval_store

    path = tmp_path / "cao.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(database_client, "DATABASE_FILE", path, raising=False)
    monkeypatch.setattr(approval_store, "DATABASE_FILE", path, raising=False)

    from cli_agent_orchestrator.api.main import app

    # base_url="http://localhost" because a host-header middleware rejects TestClient's default
    # "testserver" with a bare 400 "Invalid host header" — the same shape every test/api/ client uses.
    return TestClient(app, base_url="http://localhost")


def test_granting_an_unapproved_plan_approves_it(client):
    response = client.post("/workflows/plans/approve", json={"plan_id": "plan-v1:aaa"})
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is True
    assert body["changed"] is True
    assert body["plan_id"] == "plan-v1:aaa"
    assert body["approved_at"]
    assert body["approved_by"]


def test_a_repeat_grant_reports_the_original_approver_and_timestamp(client, monkeypatch):
    """WRITE-ONCE, made legible. A fresh timestamp here would be a lie about when it was approved."""
    monkeypatch.setattr(approval_provenance, "local_account", lambda: "first-account")
    first = client.post("/workflows/plans/approve", json={"plan_id": "plan-v1:bbb"}).json()

    monkeypatch.setattr(approval_provenance, "local_account", lambda: "second-account")
    second = client.post("/workflows/plans/approve", json={"plan_id": "plan-v1:bbb"}).json()

    assert second["approved"] is True
    assert second["changed"] is False, "a repeat grant must report that it changed nothing"
    assert second["approved_by"] == "first-account", "the original approver must survive"
    assert second["approved_at"] == first["approved_at"], "the original timestamp must survive"


def test_the_plan_id_is_stored_and_echoed_verbatim(client):
    """A normalisation is how two distinct plans could come to share one approval."""
    for value in ("plan-v1:abc", "v2:legacy-looking", "no-scheme", "MiXeD-v1:CaSe"):
        body = client.post("/workflows/plans/approve", json={"plan_id": value}).json()
        assert body["plan_id"] == value

        from cli_agent_orchestrator.services import approval_store

        record = approval_store.get_approval(value)
        assert record is not None and record.plan_id == value


def test_an_empty_plan_id_is_rejected_and_writes_nothing(client):
    from cli_agent_orchestrator.services import approval_store

    for empty in ("", "   "):
        response = client.post("/workflows/plans/approve", json={"plan_id": empty})
        assert response.status_code == 400
        assert approval_store.is_approved(empty) is False


def test_a_caller_supplied_approved_by_is_ignored(client):
    """Accepting it would be the appearance of accountability with none of the substance."""
    body = client.post(
        "/workflows/plans/approve",
        json={"plan_id": "plan-v1:ccc", "approved_by": "someone-else-entirely"},
    ).json()
    assert body["approved_by"] != "someone-else-entirely"


# ---------------------------------------------------------------------------
# The scope — the control that makes the MCP decision real
# ---------------------------------------------------------------------------


def test_the_approve_endpoint_requires_the_admin_scope():
    """Asserted on the ROUTE's dependencies, not on behaviour.

    ``require_any_scope`` is default-off, so a behavioural test would pass against ANY scope choice in
    a default install. The property that matters is which scopes the route declares.
    """
    import inspect

    from cli_agent_orchestrator.api.main import approve_workflow_plan_endpoint
    from cli_agent_orchestrator.security.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE

    source = inspect.getsource(approve_workflow_plan_endpoint)
    assert "require_any_scope(SCOPE_ADMIN)" in source, (
        "approving must require cao:admin ONLY. Widening it to the sibling WRITE, ADMIN pattern "
        "would let a write-scoped agent token grant approvals directly over HTTP, which is exactly "
        "what removing the MCP grant tool was meant to prevent."
    )
    assert SCOPE_ADMIN == "cao:admin"
    assert SCOPE_WRITE != SCOPE_ADMIN and SCOPE_READ != SCOPE_ADMIN


def test_no_mcp_tool_can_grant_an_approval():
    """The absence IS the control, so it needs a test — absences do not fail on their own."""
    from cli_agent_orchestrator.mcp_server import server

    tool_names = {name for name in dir(server) if not name.startswith("_")}
    for forbidden in (
        "workflow_approve",
        "workflow_approve_plan",
        "approve_plan",
        "workflow_grant_approval",
        "grant_plan_approval",
    ):
        assert forbidden not in tool_names, (
            f"{forbidden} must not exist: an agent that can approve the plan it just wrote makes "
            "the approval gate decorative in exactly the case it was designed for"
        )


def test_the_mcp_report_description_names_the_stale_hash_gap():
    """The description is MACHINE-READ metadata an agent uses to decide what to call.

    A description implying stale-hash rejection would cause an agent to rely on a check that never
    runs. Two of Bolt 1C's five false surfaces were prioritised for exactly this reason.
    """
    from cli_agent_orchestrator.mcp_server.server import workflow_plan_approval

    doc = workflow_plan_approval.__doc__ or ""
    assert "stale" in doc.lower() and "not" in doc.lower()
    assert "NO TOOL THAT GRANTS" in doc.upper() or "no tool that grants" in doc.lower()
    assert "off by default" in doc.lower(), "enforcement's default must not be left to be guessed"


# ---------------------------------------------------------------------------
# The read endpoint the MCP tool consults
# ---------------------------------------------------------------------------


def test_the_plan_report_distinguishes_no_plan_id_from_unapproved(client):
    """ "This run has no plan identifier" and "this plan is not approved" need different actions."""
    from cli_agent_orchestrator.clients import database as database_client
    from cli_agent_orchestrator.services import workflow_journal

    database_client.init_db()
    workflow_journal.insert_run(
        "run-yaml", "wf", "{}", "{}", "RUNNING", "2026-08-19T00:00:00+00:00"
    )
    workflow_journal.insert_run(
        "run-script",
        "wf",
        "{}",
        "{}",
        "RUNNING",
        "2026-08-19T00:00:00+00:00",
        "script",
        "1",
        json.dumps({"plan_id": "plan-v1:reported"}),
    )

    yaml_report = client.get("/workflows/runs/run-yaml/plan").json()
    assert yaml_report["plan_id"] is None
    assert yaml_report["approved"] is None, "None, not False — there is no plan to approve"

    script_report = client.get("/workflows/runs/run-script/plan").json()
    assert script_report["plan_id"] == "plan-v1:reported"
    assert script_report["approved"] is False, "a real plan_id with no approval is False, not None"


def test_the_plan_report_404s_on_an_unknown_run(client):
    assert client.get("/workflows/runs/no-such-run/plan").status_code == 404
