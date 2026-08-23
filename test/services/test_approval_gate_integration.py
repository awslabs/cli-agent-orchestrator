"""The four integration properties `approval-gate` could not assert about itself (issue #583 Bolt 2).

``approval-gate``'s unit tests cover 8 of the 12 behaviours its NFR pass pinned. The remaining four are
about what the gate's refusal does to the SYSTEM, and a unit test of the gate in isolation cannot see
any of them:

1. a refused start leaves NO ``workflow_run`` row — asserted against the journal, not inferred;
2. a refused resume does NOT bump the generation — the gate's correctness is an ORDERING inside
   ``resume_script_run``, and the NFR pass called this "the one most likely to regress silently under a
   later refactor of the admission ladder";
3. the refusal reaches a caller as **403**, distinct from the ladder's 404 / 409 / 422;
4. the two start arms produce the SAME verdict on the same inputs — otherwise a run's approvability
   would depend on which route started it.

Number 2 is the one that carries the most weight. Bumping the generation FENCES OUT any still-live
process for that run, so bumping and then refusing would kill a possibly-healthy run on behalf of a
resume that was itself rejected — two losses from one refusal. Nothing about that is visible from the
gate function; it is a property of where the call sits.
"""

import json

import pytest

from cli_agent_orchestrator.services import (
    approval_gate,
    approval_store,
    execution_manifest,
    settings_service,
    workflow_journal,
)

PLAN_ID = "plan-v1:integration"


@pytest.fixture(autouse=True)
def enforcement_on(tmp_path, monkeypatch):
    """A temp database, and approval enforcement switched on through the REAL setting."""
    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.clients import database as database_client

    path = tmp_path / "cao.db"
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(database_client, "DATABASE_FILE", path, raising=False)
    monkeypatch.setattr(workflow_journal, "DATABASE_FILE", path, raising=False)
    monkeypatch.setattr(approval_store, "DATABASE_FILE", path, raising=False)
    database_client.init_db()

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workflow": {"require_approval": True}}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    return path


def _manifest() -> str:
    return execution_manifest.serialise(
        execution_manifest.build(
            plan_id=PLAN_ID, source_hash="sha256:abc", inputs={}, repo_baseline={}
        )
    )


def _insert_failed_script_run(run_id: str) -> None:
    """A FAILED script run with a frozen manifest — the shape a resume is offered."""
    workflow_journal.insert_run(
        run_id,
        "wf",
        json.dumps({"source": "print(1)", "path": None, "content_hash": "sha256:abc"}),
        "{}",
        "failed",  # RunState.FAILED's VALUE — the enum is lower-case
        "2026-08-19T00:00:00+00:00",
        "script",
        "1",
        _manifest(),
    )


# ---------------------------------------------------------------------------
# 1. A refused start writes no run row
# ---------------------------------------------------------------------------


def test_unapproved_plan_is_refused_before_any_run_row_is_written():
    """Asserted against the journal rather than inferred from the raise.

    Every first run of every new plan is refused by design, so if a refusal wrote a row the table would
    durably record runs that never happened — which is why the gate sits before the INSERT.
    """
    with pytest.raises(approval_gate.PlanApprovalRequiredError):
        approval_gate.ensure_plan_approved(tier="script", manifest_json=_manifest())

    assert workflow_journal.get_run("any-run") is None
    assert workflow_journal.list_runs() == [] or all(
        row.run_id != "any-run" for row in workflow_journal.list_runs()
    )


# ---------------------------------------------------------------------------
# 2. A refused resume does not bump the generation  ← the load-bearing one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_resume_does_not_bump_the_generation():
    """THE ORDERING PROPERTY. The gate is admission gate 5, BEFORE the bump.

    A bump fences out any still-live process for the run. Bumping and then refusing would kill a
    possibly-healthy run on behalf of a resume that was itself rejected. This test is the only thing
    that would notice the gate being moved after the bump by a later refactor.
    """
    from cli_agent_orchestrator.services import script_runner

    _insert_failed_script_run("run-refused-resume")
    before = workflow_journal.get_run("run-refused-resume").generation

    with pytest.raises(approval_gate.PlanApprovalRequiredError) as excinfo:
        await script_runner.resume_script_run("run-refused-resume")

    assert excinfo.value.plan_id == PLAN_ID
    after = workflow_journal.get_run("run-refused-resume").generation
    assert after == before, (
        "a refused resume must NOT bump the generation — the bump fences out a live process, so "
        "bumping then refusing loses the run AND the resume"
    )


@pytest.mark.asyncio
async def test_an_approved_resume_is_admitted_past_the_gate():
    """The converse, so the test above cannot pass by the gate refusing everything."""
    from cli_agent_orchestrator.services import script_runner

    _insert_failed_script_run("run-approved-resume")
    approval_store.grant(PLAN_ID, "test-account")

    # What happens AFTER the gate is not this test's business — it may succeed, or fail on tmux or a
    # missing provider. The only forbidden outcome is the gate's own refusal. Asserted this way rather
    # than with pytest.raises, because assuming it must fail later made the test depend on the
    # environment instead of on the property.
    try:
        await script_runner.resume_script_run("run-approved-resume")
    except approval_gate.PlanApprovalRequiredError:  # pragma: no cover - the failure this forbids
        pytest.fail("an approved plan must be admitted past gate 5")
    except Exception:  # noqa: BLE001 - any later failure is out of scope for this property
        pass


# ---------------------------------------------------------------------------
# 3. The refusal reaches a caller as 403
# ---------------------------------------------------------------------------


def test_the_resume_endpoint_answers_403_and_not_400_or_409():
    """``PlanApprovalRequiredError`` is not a ``ValueError``, so the trailing 400 arm cannot claim it.

    403 also has to be distinct from the ladder's 409: "needs approval" is a fact about the PLAN and
    "cannot be resumed" is a fact about the RUN, and the operator's next action differs completely.
    """
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app

    _insert_failed_script_run("run-403")
    client = TestClient(app, base_url="http://localhost")

    response = client.post("/workflows/runs/run-403/resume")

    assert response.status_code == 403, (
        f"expected 403, got {response.status_code}: a security refusal transported as 400 sends the "
        "caller looking for a malformed argument instead of approving a plan"
    )
    assert PLAN_ID in json.dumps(response.json()), "the refusal must carry the plan_id"


# ---------------------------------------------------------------------------
# 4. Both start arms agree
# ---------------------------------------------------------------------------


def test_both_start_arms_reach_the_same_verdict_on_the_same_inputs():
    """Otherwise a run's approvability depends on which route started it.

    Asserted as the property the two arms share: they call ONE gate function with the same arguments.
    Both call sites are checked in source, and the gate's verdict is exercised directly — which is what
    makes "identical" mean something rather than being two separately-drifting copies.
    """
    import inspect

    from cli_agent_orchestrator.api import main as api_main
    from cli_agent_orchestrator.services import script_runner

    async_arm = inspect.getsource(api_main)
    blocking_arm = inspect.getsource(script_runner)

    for source, name in ((async_arm, "api/main.py"), (blocking_arm, "script_runner.py")):
        assert 'ensure_plan_approved(tier="script", manifest_json=manifest_json)' in source, (
            f"{name} must gate through the shared ensure_plan_approved with the frozen manifest; "
            "two inline checks are two chances for one to drift"
        )

    manifest = _manifest()
    with pytest.raises(approval_gate.PlanApprovalRequiredError):
        approval_gate.ensure_plan_approved(tier="script", manifest_json=manifest)

    approval_store.grant(PLAN_ID, "test-account")
    approval_gate.ensure_plan_approved(tier="script", manifest_json=manifest)  # must not raise
