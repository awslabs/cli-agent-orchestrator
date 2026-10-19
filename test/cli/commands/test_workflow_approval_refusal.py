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
from pathlib import Path
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
        wf.api_http,
        "post",
        lambda *a, **k: _refusal_response(403, "approval_required", PLAN_ID),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert result.exit_code != 0
    assert PLAN_ID in result.output
    assert "needs human approval" in result.output
    assert "an operator approves it with" in result.output
    assert "Approve it with:" not in result.output


def test_a_refusal_says_the_grant_is_not_available_from_here(runner, monkeypatch):
    """So an operator (or an agent reading the terminal) does not go hunting for a flag that grants it.
    There is deliberately no such flag and no such tool."""
    monkeypatch.setattr(
        wf.api_http,
        "post",
        lambda *a, **k: _refusal_response(403, "approval_required", PLAN_ID),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert "human decision" in result.output


def test_a_failed_freeze_does_not_offer_an_approval(runner, monkeypatch):
    """503, ``plan_id: None`` -- there is nothing to approve, and saying otherwise is the misleading
    failure this distinction exists to prevent."""
    monkeypatch.setattr(
        wf.api_http,
        "post",
        lambda *a, **k: _refusal_response(503, "plan_identity_unavailable", None),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert result.exit_code != 0
    assert "cao workflow approve" not in result.output, (
        "no identifier exists to approve; offering the command sends the operator to grant something "
        "that cannot be named"
    )
    assert "Retry the run" not in result.output
    assert "The reason above says what to change" in result.output


def test_the_resume_verb_reports_a_refusal_the_same_way(runner, monkeypatch):
    """Resume is gated too -- the fifth check in ``resume_script_run``'s admission ladder -- and an
    operator resuming a run should not have to learn a second vocabulary for the same refusal."""
    monkeypatch.setattr(
        wf.api_http,
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
        wf.api_http,
        "post",
        lambda *a, **k: SimpleNamespace(
            status_code=400, json=lambda: payload, text=json.dumps(payload), headers={}
        ),
    )

    result = runner.invoke(wf.workflow, ["run", "wf"])

    assert "workflow inputs exceed 65536 bytes" in result.output
    assert "cao workflow approve" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["run", "wf"],
        ["run", "wf", "--wait"],
        ["run", "wf", "--detach"],
        ["resume", "wf"],
        ["wait", "wf"],
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("server_variant", ["current", "old", "command-first"])
def test_approval_output_has_one_human_command_for_all_paths(
    runner, monkeypatch, args, server_variant, as_json
):
    command = f"cao workflow approve {PLAN_ID}"
    human_message = (
        f"Plan '{PLAN_ID}' needs human approval before it can run: "
        + f"an operator approves it with `{command}`, then runs it again."
    )
    if server_variant == "current":
        message = human_message
    elif server_variant == "old":
        message = f"Plan '{PLAN_ID}' has not been approved."
    else:
        message = f"{command}\nThen run again."
    expected = human_message + "\nApproval is a human decision; an agent must not grant it."
    payload = {"detail": {"kind": "approval_required", "plan_id": PLAN_ID, "message": message}}
    response = SimpleNamespace(
        status_code=403, json=lambda: payload, text=json.dumps(payload), headers={}
    )
    monkeypatch.setattr(wf.api_http, "post", lambda *a, **k: response)
    monkeypatch.setattr(wf.api_http, "get", lambda *a, **k: response)

    result = runner.invoke(wf.workflow, args + (["--json"] if as_json else []))

    assert result.exit_code == 1
    if as_json and args[0] != "wait":
        assert json.loads(result.output) == {
            "ok": False,
            "class": "approval_required",
            "plan_id": PLAN_ID,
            "error": expected,
        }
    else:
        assert result.output == "Error: " + expected + "\n"
    assert result.output.count(command) == 1
    assert "Approve it with:" not in result.output
    for line in result.output.splitlines():
        assert not line.removeprefix("Error: ").startswith("cao workflow approve")


def test_both_skill_examples_frame_approval_as_a_user_decision():
    root = Path(__file__).resolve().parents[3]
    for path in [
        root / "skills/cao-workflow/SKILL.md",
        root / "src/cli_agent_orchestrator/skills/cao-workflow/SKILL.md",
    ]:
        source = path.read_text()
        assert "(you must not; no tool does)" in source
        assert "you cannot, and no tool can" not in source
        assert "Approve it with:" not in source
        assert "needs human approval before it can run: an operator approves it with" in source


def test_identity_refusal_does_not_invent_retry(runner, monkeypatch):
    message = "CAO could not compute a plan identifier. Gitignore the large untracked files."
    payload = {"detail": {"kind": "plan_identity_unavailable", "plan_id": None, "message": message}}
    response = SimpleNamespace(
        status_code=503, json=lambda: payload, text=json.dumps(payload), headers={}
    )
    monkeypatch.setattr(wf.api_http, "post", lambda *args, **kwargs: response)
    result = runner.invoke(wf.workflow, ["run", "wf"])
    assert result.exit_code == 1
    assert message in result.output
    assert "Retry" not in result.output
    assert "The reason above says what to change" in result.output
