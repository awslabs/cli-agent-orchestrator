"""`cao workflow run` / `resume` turn a refusal into the next action (issue #583 Bolt 3).

The APPROVE step of FR-10's sequence, at the human surface. Before this unit a refusal reached the
operator as whatever ``_extract_detail`` made of it, with no distinction between "approve this plan"
and "CAO failed its own freeze" beyond a status code the CLI never showed.

The two that carry the load:

* ``test_a_refusal_names_the_exact_approve_command`` -- a control that refuses without saying what to
  approve cannot be operated, which ``approval_gate.py``:179 already says in as many words.
* ``test_a_failed_freeze_does_not_offer_an_approval`` -- the opposite error, and the more misleading
  one: sending an operator to approve a plan whose identifier was never readable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import workflow as wf

PLAN_ID = "plan-v1:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _refusal_response(status_code: int, kind: str, plan_id):
    payload = {
        "detail": {
            "kind": kind,
            "plan_id": plan_id,
            "message": (
                f"Plan '{plan_id}' has not been approved. Approve this plan identifier and run again."
                if kind == "approval_required"
                else "This script-tier run has no readable plan identifier in its frozen manifest."
            ),
        }
    }
    return SimpleNamespace(
        status_code=status_code, json=lambda: payload, text=json.dumps(payload), headers={}
    )


@pytest.fixture()
def runner():
    return CliRunner()


def test_a_refusal_names_the_exact_approve_command(runner, monkeypatch):
    """The identifier AND the command, so the operator's next act is a copy-paste rather than a search.

    The first run of a new plan is refused by design, so this is the ORDINARY path for a newly authored
    workflow -- not an edge case.
    """
    monkeypatch.setattr(
        wf.requests,
        "post",
        lambda *a, **k: _refusal_response(403, "approval_required", PLAN_ID),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert result.exit_code != 0
    assert PLAN_ID in result.output
    assert f"cao workflow approve {PLAN_ID}" in result.output
    assert "has not been approved" in result.output, "the server's own sentence is preserved"


def test_a_refusal_says_the_grant_is_not_available_from_here(runner, monkeypatch):
    """So an operator (or an agent reading the terminal) does not go hunting for a flag that grants it.
    There is deliberately no such flag and no such tool."""
    monkeypatch.setattr(
        wf.requests,
        "post",
        lambda *a, **k: _refusal_response(403, "approval_required", PLAN_ID),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert "human decision" in result.output


def test_a_failed_freeze_does_not_offer_an_approval(runner, monkeypatch):
    """503, ``plan_id: None`` -- there is nothing to approve, and saying otherwise is the misleading
    failure this distinction exists to prevent."""
    monkeypatch.setattr(
        wf.requests,
        "post",
        lambda *a, **k: _refusal_response(503, "plan_identity_unavailable", None),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert result.exit_code != 0
    assert "cao workflow approve" not in result.output, (
        "no identifier exists to approve; offering the command sends the operator to grant something "
        "that cannot be named"
    )
    assert "Retry" in result.output or "retry" in result.output
    assert "CAO-side" in result.output


def test_the_resume_verb_reports_a_refusal_the_same_way(runner, monkeypatch):
    """Resume is gated too -- the fifth check in ``resume_script_run``'s admission ladder -- and an
    operator resuming a run should not have to learn a second vocabulary for the same refusal."""
    monkeypatch.setattr(
        wf.requests,
        "post",
        lambda *a, **k: _refusal_response(403, "approval_required", PLAN_ID),
    )

    result = runner.invoke(wf.workflow, ["resume", "some-run"])

    assert f"cao workflow approve {PLAN_ID}" in result.output


def test_an_ordinary_error_is_unaffected(runner, monkeypatch):
    """The refusal arm must not swallow the other failures.

    A string ``detail`` is still the common case on this API, and it is also what an OLDER server sends
    -- so this asserts the change is additive rather than a replacement.
    """
    payload = {"detail": "workflow inputs exceed 65536 bytes"}
    monkeypatch.setattr(
        wf.requests,
        "post",
        lambda *a, **k: SimpleNamespace(
            status_code=400, json=lambda: payload, text=json.dumps(payload), headers={}
        ),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert "workflow inputs exceed 65536 bytes" in result.output
    assert "cao workflow approve" not in result.output


def test_approve_forwards_the_local_bearer(runner, monkeypatch):
    """Approval remains admin-only, with its configured bearer forwarded verbatim."""
    captured = {}

    def capture(*args, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "plan_id": PLAN_ID,
                "approved": True,
                "changed": False,
                "approved_at": "2026-09-16T00:00:00Z",
                "approved_by": "tester",
            },
            text="",
            headers={},
        )

    monkeypatch.setenv("AUTH0_DOMAIN", "auth.test")
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "test-token")
    monkeypatch.setattr(wf.requests, "post", capture)

    result = runner.invoke(wf.workflow, ["approve", PLAN_ID])

    assert result.exit_code == 0, result.output
    assert captured["headers"] == {"Authorization": "Bearer test-token"}


def test_approve_omits_authorization_without_a_local_bearer(runner, monkeypatch):
    """Auth-off/public servers receive no synthetic Authorization header."""
    captured = {}

    def capture(*args, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "plan_id": PLAN_ID,
                "approved": True,
                "changed": False,
                "approved_at": "2026-09-16T00:00:00Z",
                "approved_by": "tester",
            },
            text="",
            headers={},
        )

    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr(wf.requests, "post", capture)

    result = runner.invoke(wf.workflow, ["approve", PLAN_ID])

    assert result.exit_code == 0, result.output
    assert captured["headers"] is None
