"""The structured approval-refusal `detail` (issue #583 Bolt 3, ``authoring-sequence``).

FR-10 states the sequence as "describe -> author -> validate -> PRESENT PLAN -> APPROVE -> run ->
observe". That centre link was unreachable before this unit, and not for one reason but three
composing:

1. A refused start writes NO run row (``script_runner``:1183 gate, :1215 insert) -- deliberately.
2. So ``workflow_plan_approval(run_id)``, the only surface reporting a ``plan_id`` as a FIELD, has
   nothing to read on the first run of a new plan -- which by design is every newly authored one.
3. The handlers transported ``detail=str(e)``, discarding the ``plan_id`` the exception carries
   expressly so it can travel.

What was left was scraping an English sentence, which ``services.md`` forbids: "a caller that can read
the body must branch on the field, never regex-scrape the message."

Three tests carry this module's load:

* ``test_the_key_set_is_exact_on_both_statuses`` -- an EXACT set, so an unintentional field (the
  manifest, the resolved inputs, the spec path) shows up as a failure. The inputs are journaled in
  plaintext and may name paths; the spec path would disclose filesystem layout to a caller that only
  asked to run something.
* ``test_the_plan_id_round_trips_byte_identically`` -- the caller's next act is to pass this value to
  ``cao workflow approve``, which records why: "a normalisation is how two distinct plans could share
  one approval."
* ``test_plan_id_is_present_and_null_on_the_503`` -- present-and-NULL, never absent, so one reader
  handles both statuses without an isinstance or status check first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.workflow import ScriptSpec
from cli_agent_orchestrator.services import approval_gate, script_runner

PLAN_ID = "plan-v1:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture()
def client():
    return TestClient(app, base_url="http://localhost")


@pytest.fixture()
def spec(monkeypatch):
    s = ScriptSpec(
        name="seq", path="/tmp/seq.py", source="def main():\n    pass\n", content_hash="deadbeef"
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: s,
    )
    return s


def _refuse_with(monkeypatch, error: Exception, *, attr: str = "run_script_workflow"):
    async def _boom(*a, **k):
        raise error

    monkeypatch.setattr(script_runner, attr, _boom)


def _unapproved() -> approval_gate.PlanApprovalRequiredError:
    return approval_gate.PlanApprovalRequiredError(
        f"Plan '{PLAN_ID}' has not been approved. Approve this plan identifier and run again.",
        plan_id=PLAN_ID,
    )


def _no_identity() -> approval_gate.PlanIdentityUnavailableError:
    return approval_gate.PlanIdentityUnavailableError(
        "This script-tier run has no readable plan identifier in its frozen execution manifest."
    )


# ---------------------------------------------------------------------------
# The three load-bearing properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_factory,expected_status,expected_kind",
    [
        (_unapproved, 403, "approval_required"),
        (_no_identity, 503, "plan_identity_unavailable"),
    ],
)
def test_the_key_set_is_exact_on_both_statuses(
    client, spec, monkeypatch, error_factory, expected_status, expected_kind
):
    """EXACT, not a subset check.

    Extend this set when a field is added deliberately; do NOT relax it to a subset check. An exact
    set is precisely what makes an UNINTENTIONAL field fail -- and the tempting additions here are the
    dangerous ones (the manifest, the resolved inputs, the source hash, the spec path). A caller that
    only asked to run something must not learn the filesystem layout from being refused.
    """
    _refuse_with(monkeypatch, error_factory())

    response = client.post("/workflows/runs", json={"name_or_path": "seq", "inputs": {}})

    assert response.status_code == expected_status
    detail = response.json()["detail"]
    assert set(detail) == {"kind", "plan_id", "message"}, (
        "exactly three keys. If you are adding one deliberately, extend this set rather than "
        "loosening the check -- the exactness is what catches an accidental disclosure."
    )
    assert detail["kind"] == expected_kind


def test_the_plan_id_round_trips_byte_identically(client, spec, monkeypatch):
    """No normalisation, ever -- not case, not stripping, not truncation.

    ``cao workflow approve`` records the constraint inbound: "a normalisation is how two distinct
    plans could share one approval." Outbound the caller's next act is to pass this to that command,
    so a transformation here produces either a rejected approval or one matching the wrong plan.
    """
    _refuse_with(monkeypatch, _unapproved())

    response = client.post("/workflows/runs", json={"name_or_path": "seq", "inputs": {}})

    assert response.json()["detail"]["plan_id"] == PLAN_ID


def test_plan_id_is_present_and_null_on_the_503(client, spec, monkeypatch):
    """Present-and-null, never absent, so ONE reader handles both statuses.

    Omitting the key would force every caller to check the status before it could read a field --
    which relocates the branch-on-shape problem rather than solving it.
    """
    _refuse_with(monkeypatch, _no_identity())

    response = client.post("/workflows/runs", json={"name_or_path": "seq", "inputs": {}})

    detail = response.json()["detail"]
    assert "plan_id" in detail, "absent is wrong; null is right"
    assert detail["plan_id"] is None


# ---------------------------------------------------------------------------
# All three routes, because a fix that lands on one arm is this shape's classic defect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_factory,expected_status,expected_kind,expected_plan_id",
    [
        (_unapproved, 403, "approval_required", PLAN_ID),
        (_no_identity, 503, "plan_identity_unavailable", None),
    ],
)
def test_the_async_submit_arm_carries_the_same_shape(
    client, spec, monkeypatch, error_factory, expected_status, expected_kind, expected_plan_id
):
    """The two start arms must agree.

    ``ensure_plan_approved``'s docstring gives the reason it is one function rather than three inline
    checks: "both start arms must agree or a run's approvability would depend on which route started
    it." The same applies to how the refusal is REPORTED -- an agent on the async path must not get a
    different shape from one on the blocking path.

    NOTE THE DIFFERENT PATCH TARGET, which is the thing worth knowing about this route: the submit arm
    calls ``ensure_plan_approved`` INLINE in the handler, while the blocking arm reaches it through
    ``script_runner.run_script_workflow``. Patching the runner (as the blocking tests do) does not
    reach this arm at all -- it returns 404 instead, which reads like a routing problem rather than a
    missed gate.
    """
    from cli_agent_orchestrator.api import main as api_main

    def _boom(*a, **k):
        raise error_factory()

    monkeypatch.setattr(api_main.approval_gate, "ensure_plan_approved", _boom)

    response = client.post("/workflows/runs:submit", json={"name_or_path": "seq", "inputs": {}})

    assert response.status_code == expected_status, response.text
    detail = response.json()["detail"]
    assert detail["kind"] == expected_kind
    assert detail["plan_id"] == expected_plan_id
    assert set(detail) == {"kind", "plan_id", "message"}


# ---------------------------------------------------------------------------
# The prose is preserved, and nothing from the request reaches it
# ---------------------------------------------------------------------------


def test_the_message_is_the_exception_text_verbatim(client, spec, monkeypatch):
    """``message`` is ``str(error)`` unabridged -- the human-facing sentence and the machine-facing
    field are one fact at two grains, which is what stops them drifting."""
    error = _unapproved()
    _refuse_with(monkeypatch, error)

    response = client.post("/workflows/runs", json={"name_or_path": "seq", "inputs": {}})

    assert response.json()["detail"]["message"] == str(error)


def test_nothing_from_the_request_is_interpolated_into_the_message(client, spec, monkeypatch):
    """This body is rendered into a terminal and into an agent's context, so a caller-controlled
    substring here would be an injection surface into whatever reads it."""
    _refuse_with(monkeypatch, _unapproved())

    response = client.post(
        "/workflows/runs",
        json={"name_or_path": "seq", "inputs": {}, "run_id": "INJECTED-RUN-ID"},
    )

    assert "INJECTED-RUN-ID" not in response.json()["detail"]["message"]
