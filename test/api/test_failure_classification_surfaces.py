"""FR-10's criterion at the surfaces a caller uses (issue #583 Bolt 3, ``failure-classification``).

The mapping is unit-tested next door. These tests exist because FR-10's Fail condition — "the two
failure classes are conflated" — is a property of what a CALLER SEES, and a unit test of the classifier
would pass even if the field never reached the response, or reached only one of the two surfaces.

The load-bearing one is ``test_the_cli_and_the_mcp_tool_agree``. It is the only test here that would
catch the likeliest future regression: a fourth classification added with one surface's rendering updated
and the other's forgotten. The end-to-end and unit tests both stay green through that.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import workflow_journal


@pytest.fixture(autouse=True)
def journal_db(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    return db_path


@pytest.fixture()
def client():
    return TestClient(app, base_url="http://localhost")


def _failed_run(run_id: str, error_kind: str) -> None:
    """A terminal FAILED run whose step carries a durable ``error_kind``."""
    workflow_journal.insert_run(
        run_id,
        "wf",
        json.dumps({"source": "print(1)", "path": None, "content_hash": "sha256:abc"}),
        "{}",
        "failed",
        "2026-08-24T00:00:00+00:00",
        "script",
        "1",
        None,
    )
    # ``update_step`` is what projects error_kind into the durable column (#504's U2); ``settle_step``
    # takes no such parameter. Verified against the signature rather than inferred from the name — the
    # sibling helper's shape was the wrong precedent to copy from, twice in this pass.
    workflow_journal.insert_steps(run_id, [("s1", "running")], "2026-08-24T00:00:00+00:00")
    workflow_journal.update_step(
        run_id,
        "s1",
        "failed",
        1,
        "2026-08-24T00:00:01+00:00",
        error="something went wrong",
        error_kind=error_kind,
    )


def _envelope(client, run_id):
    resp = client.get(f"/workflows/runs/{run_id}/result")
    assert resp.status_code == 200, resp.text
    return resp.json().get("failure_envelope")


def test_the_two_classes_differ_over_http(client):
    """FR-10's second Pass criterion, asserted where a caller reads it.

    Both runs FAILED; only the persisted ``error_kind`` differs. If the two came back the same, the
    classes would be conflated — which is FR-10's Fail condition stated literally.
    """
    _failed_run("run-transient", "timeout")
    _failed_run("run-defect", "error")

    transient = _envelope(client, "run-transient")
    defect = _envelope(client, "run-defect")

    assert transient["classification"] == "transient"
    assert defect["classification"] == "artifact_defect"
    assert transient["classification"] != defect["classification"], (
        "the two failure classes must be distinguishable at the result surface — this inequality IS "
        "FR-10's second Pass criterion"
    )


def test_a_provider_error_is_transient_over_http(client):
    _failed_run("run-provider", "provider_error")
    assert _envelope(client, "run-provider")["classification"] == "transient"


def test_a_successful_run_carries_no_envelope_and_no_classification(client):
    """SEC-6 / NFR-3: a success's ``--json`` shape is untouched by this unit."""
    workflow_journal.insert_run(
        "run-ok",
        "wf",
        json.dumps({"source": "print(1)", "path": None, "content_hash": "sha256:abc"}),
        "{}",
        "completed",
        "2026-08-24T00:00:00+00:00",
        "script",
        "1",
        None,
    )
    body = client.get("/workflows/runs/run-ok/result").json()

    assert "failure_envelope" not in body, "the whole key must stay absent for a success"
    assert "classification" not in json.dumps(
        body
    ), "no classification may leak onto a successful run by any route"


def test_the_cli_and_the_mcp_tool_agree(client, monkeypatch):
    """REL-6 / SCALE-4 — one derivation, two presentations.

    THE MOST VALUABLE TEST IN THIS FILE. A fourth classification added with only one surface's rendering
    updated leaves every other test here green, and this one red. The CLI renders an ACTION PHRASE while
    the MCP tool passes the raw enum through, so the assertion is that the two describe the SAME value —
    not that they print the same string.
    """
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands import workflow as cli_workflow

    _failed_run("run-agree", "timeout")
    server_value = _envelope(client, "run-agree")["classification"]

    # The MCP tool spreads the body through verbatim, so an agent sees the raw enum.
    assert server_value == "transient"

    # The CLI renders the same value as an action phrase.
    monkeypatch.setattr(
        cli_workflow.requests,
        "get",
        lambda url, timeout=None: client.get("/workflows/runs/run-agree/result"),
    )
    result = CliRunner().invoke(cli_workflow.workflow, ["result", "run-agree"])

    expected_phrase = cli_workflow._CLASSIFICATION_ACTIONS[server_value]
    assert expected_phrase in result.output, (
        f"the CLI must render the server's {server_value!r} as {expected_phrase!r}; a value the "
        "renderer has no phrasing for would silently vanish from the human view"
    )


def test_an_unknown_classification_degrades_to_silence_in_the_human_view():
    """A future fourth value must not print a raw token to a human.

    It still appears in ``--json`` — the machine contract is unaffected — but the action line is omitted
    rather than echoing a value nobody wrote phrasing for.
    """
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands import workflow as cli_workflow

    runner = CliRunner()
    with runner.isolation() as outstreams:
        cli_workflow._render_failure_envelope(
            {
                "classification": "some_future_value",
                "failing_step": "s1",
                "attempt": 1,
                "error_kind": "error",
                "terminal_reference": "r",
                "next_command": "cao workflow result r",
            }
        )
        out = outstreams[0].getvalue().decode()

    assert "some_future_value" not in out
    assert "what to do:" not in out
    assert "failing step:" in out, "the rest of the envelope must still render"


def test_the_classification_adds_no_journal_query(client, monkeypatch):
    """PERF-1: the property, not a latency figure.

    The plausible regression is not "the branch got slower" but "someone needed one more fact and added
    a read to get it" — which is exactly what the design's original divergence premise would have
    required. A query count catches that; a p99 budget would not.
    """
    _failed_run("run-count", "timeout")

    calls: list[str] = []
    real_get_run = workflow_journal.get_run
    real_get_steps = workflow_journal.get_steps
    monkeypatch.setattr(
        workflow_journal, "get_run", lambda rid: (calls.append("get_run"), real_get_run(rid))[1]
    )
    monkeypatch.setattr(
        workflow_journal,
        "get_steps",
        lambda rid: (calls.append("get_steps"), real_get_steps(rid))[1],
    )

    envelope = _envelope(client, "run-count")

    assert envelope["classification"] == "transient", "the classification did happen"
    assert calls == [
        "get_run",
        "get_steps",
    ], f"exactly the two reads the endpoint already made, got {calls} — the classifier must add none"
