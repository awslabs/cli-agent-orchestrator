"""Tests for the Bolt-3 workflow run CLI verbs (issue #312, N5).

Covers ``cao workflow run`` / ``status`` / ``cancel`` as thin HTTP clients:
happy path, error-detail surfacing, ``--input k=v`` parsing + type coercion, and
the non-zero exit on a non-COMPLETED run. ``requests`` is mocked — no server.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import _coerce, _parse_inputs, workflow
from cli_agent_orchestrator.constants import (
    MCP_REQUEST_TIMEOUT,
    WORKFLOW_POLL_INTERVAL_SECONDS,
    WORKFLOW_RUN_REQUEST_TIMEOUT,
)


@pytest.fixture
def runner():
    return CliRunner()


def _resp(status_code=200, json_body=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    return r


# ---------------------------------------------------------------------------
# --input parsing / coercion
# ---------------------------------------------------------------------------
def test_coerce_types():
    assert _coerce("true") is True
    assert _coerce("False") is False
    assert _coerce("42") == 42
    assert _coerce("hello") == "hello"
    assert _coerce("3.5") == "3.5"  # not an int -> stays string


def test_parse_inputs_ok():
    assert _parse_inputs(["a=1", "b=hi", "c=true"]) == {"a": 1, "b": "hi", "c": True}


def test_parse_inputs_missing_eq(runner):
    import click

    with pytest.raises(click.ClickException):
        _parse_inputs(["noequals"])


def _submit_resp(run_id="run1", state="running"):
    """A 202 ack from ``POST /workflows/runs:submit`` with the links map."""
    return _resp(
        202,
        {
            "run_id": run_id,
            "state": state,
            "links": {
                "self": f"/workflows/runs/{run_id}",
                "status": f"/workflows/runs/{run_id}",
                "events": f"/workflows/runs/{run_id}/events",
                "result": f"/workflows/runs/{run_id}/result",
                "cancel": f"/workflows/runs/{run_id}/cancel",
            },
        },
    )


def _snap(state, run_id="run1", current=None, steps=None):
    """A status snapshot body from ``GET /workflows/runs/{id}``."""
    return {
        "run_id": run_id,
        "state": state,
        "current_step_id": current,
        "steps": steps if steps is not None else [],
    }


# ---------------------------------------------------------------------------
# run — bare default: submit (:submit) + follow to terminal (T1/T3/T5)
# ---------------------------------------------------------------------------
def test_run_bare_follows_to_completed_exit_0(runner):
    """T1 (EC-1): bare ``run`` submits, follows the poll to ``completed``, exits 0."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.side_effect = [_resp(200, _snap("running")), _resp(200, _snap("completed"))]
        result = runner.invoke(workflow, ["run", "wf", "--input", "topic=cats"])
    assert result.exit_code == 0
    assert "run1" in result.output
    assert "completed" in result.output
    # Submit targets the async :submit spine and carries the parsed inputs.
    args, kwargs = mock_req.post.call_args
    assert args[0].endswith("/workflows/runs:submit")
    assert kwargs["json"]["inputs"] == {"topic": "cats"}


def test_run_bare_follows_to_failed_exit_1(runner):
    """T1 (EC-1): a poll settling on ``failed`` yields exit 1 (failed/cancelled -> 1)."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.return_value = _resp(200, _snap("failed"))
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 1


def test_run_non_tty_failed_run_nonzero_exit(runner):
    """T2 (EC-2, MANDATED NFR-2b): with stdout NOT a TTY (CliRunner default), a
    FAILED run still follows to terminal and still yields a NON-ZERO exit."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
        patch("cli_agent_orchestrator.cli.commands.workflow.sys.stdout.isatty", return_value=False),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.return_value = _resp(200, _snap("failed"))
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code != 0
    # Non-TTY follow emits a stable machine object carrying the terminal state.
    payload = json.loads(result.stdout)
    assert payload == {"run_id": "run1", "state": "failed"}


def test_run_json_follow_stable_and_preserves_exit(runner):
    """T3 (EC-3): ``run --json`` emits parseable JSON and the exit code still equals
    the terminal status (JSON does not drift the exit code)."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.return_value = _resp(200, _snap("completed"))
        result = runner.invoke(workflow, ["run", "wf", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"run_id": "run1", "state": "completed"}


def test_follow_renders_each_step_transition(runner):
    """FP-6 (issue #505 review): the follow loop renders STEP progress, not just run
    state.

    The bug: the printer keyed on the run state alone, and the run state is ``running``
    for the ENTIRE drive — so a 10-step, 40-minute workflow printed
    ``[running] current: (none)`` once and then nothing until it finished, looking
    identical to a hung run. ``current_step_id`` is already in every snapshot, so
    keying on the ``(state, current_step_id)`` PAIR yields real per-step progress at
    no extra request cost.

    MUTATION PROOF: revert the predicate to ``state != last_state`` and this goes RED
    (only the first line is printed; s2/s3 never appear).
    """
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
        patch("cli_agent_orchestrator.cli.commands.workflow._machine_mode", return_value=False),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.side_effect = [
            _resp(200, _snap("running", current="s1")),
            _resp(200, _snap("running", current="s2")),
            _resp(200, _snap("running", current="s3")),
            _resp(200, _snap("completed", current=None)),
        ]
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 0
    for step in ("s1", "s2", "s3"):
        assert f"[running] current: {step}" in result.output, f"step {step} was never rendered"


def test_follow_does_not_reprint_an_unchanged_step(runner):
    """FP-6 must not become chatty: repeated identical (state, step) snapshots print
    ONCE, so a 1s poll on a 10-minute step does not emit 600 identical lines."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
        patch("cli_agent_orchestrator.cli.commands.workflow._machine_mode", return_value=False),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.side_effect = [
            _resp(200, _snap("running", current="s1")),
            _resp(200, _snap("running", current="s1")),
            _resp(200, _snap("running", current="s1")),
            _resp(200, _snap("completed", current=None)),
        ]
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 0
    assert result.output.count("[running] current: s1") == 1


def test_follow_json_mode_emits_no_progress_lines(runner):
    """FP-6 must not leak human progress lines into a machine stream: under ``--json``
    the step-transition printer stays silent and stdout is exactly one JSON object."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.side_effect = [
            _resp(200, _snap("running", current="s1")),
            _resp(200, _snap("running", current="s2")),
            _resp(200, _snap("completed", current=None)),
        ]
        result = runner.invoke(workflow, ["run", "wf", "--json"])
    assert result.exit_code == 0
    assert "current:" not in result.stdout
    assert json.loads(result.stdout) == {"run_id": "run1", "state": "completed"}


def test_run_ctrl_c_detaches_without_cancel(runner):
    """T4 (CC-1, MANDATED): a KeyboardInterrupt mid-follow DETACHES — exit 0, a
    "still running" hint is printed, and NO cancel POST is issued."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch(
            "cli_agent_orchestrator.cli.commands.workflow._poll_to_terminal",
            side_effect=KeyboardInterrupt,
        ),
    ):
        mock_req.post.return_value = _submit_resp()
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 0
    assert "still running" in result.output
    assert "run1" in result.output
    # Exactly ONE POST — the submit. Cancel was NEVER called.
    assert mock_req.post.call_count == 1
    assert mock_req.post.call_args_list[0].args[0].endswith("/workflows/runs:submit")
    assert not any("cancel" in str(call.args[0]) for call in mock_req.post.call_args_list)


def test_run_prints_id_before_follow_survives_first_poll_interrupt(runner):
    """T5 (FP-1): the run id is surfaced even when the follow is interrupted on the
    FIRST poll — the handle survives the interrupt.

    Patches the request methods individually so the interrupt reaches the outer
    ``except KeyboardInterrupt`` without the whole-module mock turning the inner
    ``except RequestException`` into a non-class.
    """
    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.post",
            return_value=_submit_resp(),
        ),
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=KeyboardInterrupt,
        ),
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
        patch("cli_agent_orchestrator.cli.commands.workflow.sys.stdout.isatty", return_value=True),
    ):
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 0
    # On a TTY the id is printed up front (before the loop) AND in the detach hint.
    assert "run1" in result.output


def test_run_lost_socket_is_not_run_death(runner):
    """T6 (FP-3): a poll transport error (retry exhausted) reports lost contact and
    does NOT report the run as failed — exit reflects lost-contact, not a failure.

    Patches ``requests.post``/``requests.get`` individually (not the whole module)
    so the real ``requests.exceptions`` hierarchy stays intact for the except clause.
    """
    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.post",
            return_value=_submit_resp(),
        ),
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=requests.exceptions.ConnectionError("down"),
        ),
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 0
    assert "lost contact" in result.output
    assert "failed" not in result.output


def test_run_detach_exits_0_after_submit_no_follow(runner):
    """T7 (VR-1): ``run --detach`` returns exit 0 right after a 202, prints id +
    links, and does NOT enter the poll loop (no snapshot GET)."""
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _submit_resp()
        result = runner.invoke(workflow, ["run", "wf", "--detach"])
    assert result.exit_code == 0
    assert "run1" in result.output
    mock_req.get.assert_not_called()


def test_run_wait_uses_blocking_route_and_long_timeout(runner):
    """T8 (VR-2, C-6): ``run --wait`` POSTs the blocking ``/workflows/runs`` (NOT
    ``:submit``) with the worst-case ``WORKFLOW_RUN_REQUEST_TIMEOUT``."""
    body = {"run_id": "run1", "state": "completed", "steps": []}
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["run", "wf", "--wait"])
    assert result.exit_code == 0
    args, kwargs = mock_req.post.call_args
    assert args[0].endswith("/workflows/runs")
    assert not args[0].endswith(":submit")
    assert kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT
    assert WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT


def test_run_unknown_workflow_404(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(404, {"detail": "unknown workflow 'ghost'"})
        result = runner.invoke(workflow, ["run", "ghost"])
    assert result.exit_code != 0
    assert "unknown workflow" in result.output


def test_run_reserved_mode_501_surfaces_detail(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(501, {"detail": "mode 'parallel' is reserved"})
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code != 0
    assert "reserved" in result.output


def test_poll_interval_is_named_constant():
    """T13 (FP-5): the follow poll interval is a named constant pinned to 1.0s."""
    assert WORKFLOW_POLL_INTERVAL_SECONDS == 1.0


def test_run_follow_poll_uses_normal_timeout(runner):
    """T13 (FP-4): each follow poll uses MCP_REQUEST_TIMEOUT, not the long timeout."""
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.return_value = _resp(200, _snap("completed"))
        runner.invoke(workflow, ["run", "wf"])
    _, get_kwargs = mock_req.get.call_args
    assert get_kwargs["timeout"] == MCP_REQUEST_TIMEOUT


def test_resume_keeps_long_blocking_timeout(runner):
    """U8-T2 (TR-2): ``resume`` re-drives the whole run inline, so it POSTs the
    ``/resume`` route with the worst-case WORKFLOW_RUN_REQUEST_TIMEOUT (never the
    flat MCP_REQUEST_TIMEOUT — a 30s ceiling would report a still-running resume as
    a failure)."""
    body = {"run_id": "run1", "state": "completed", "steps": []}
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["resume", "run1"])
    assert result.exit_code == 0
    args, kwargs = mock_req.post.call_args
    assert args[0].endswith("/workflows/runs/run1/resume")
    assert kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT


# ---------------------------------------------------------------------------
# U8 (issue #505): the consolidating C-6 timeout split-guard. Asserts BOTH
# families in one place: the blocking client paths (run --wait, resume) keep the
# long WORKFLOW_RUN_REQUEST_TIMEOUT; the async client paths (:submit + every poll,
# wait, result, runs, status) use the normal MCP_REQUEST_TIMEOUT; and the constant
# relationship WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT holds so an async
# path can never silently revert to the long timeout (and a blocking path never to
# 30s). Overlaps the per-path assertions above (test_run_wait_..., _resume_...,
# _run_follow_poll_...); this test is the single split-guard that fails loudly if
# EITHER family drifts.
# ---------------------------------------------------------------------------
def test_c6_timeout_split_guard_cli(runner):
    """U8-T5 (TS-2, C-6): the CLI blocking vs async timeout split, asserted together.

    (a) constant relationship holds; (b) ``run --wait`` + ``resume`` use the long
    blocking timeout; (c) the async ``:submit`` + poll, ``wait``, ``result``,
    ``runs``, ``status`` all use the normal per-call timeout.
    """
    # (a) The regression guard on the constant relationship (C-6): the split can
    # never collapse — a blocking path can never drop to 30s, an async path can
    # never rise to the long timeout, without this assertion firing.
    assert WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT

    # (b) BLOCKING family -> the long timeout.
    body = {"run_id": "run1", "state": "completed", "steps": []}
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        runner.invoke(workflow, ["run", "wf", "--wait"])
    assert mock_req.post.call_args.kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT

    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        runner.invoke(workflow, ["resume", "run1"])
    assert mock_req.post.call_args.kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT

    # (c) ASYNC family -> the normal per-call timeout. Assert the submit POST and
    # each read GET (poll, wait, result, runs, status) all pass MCP_REQUEST_TIMEOUT.
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.post.return_value = _submit_resp()
        mock_req.get.return_value = _resp(200, _snap("completed"))
        runner.invoke(workflow, ["run", "wf"])  # bare = submit + follow
    # The submit POST and the poll GET both use the normal timeout.
    assert mock_req.post.call_args.kwargs["timeout"] == MCP_REQUEST_TIMEOUT
    assert mock_req.get.call_args.kwargs["timeout"] == MCP_REQUEST_TIMEOUT

    for argv, body_or_rows in (
        (["wait", "run1"], _snap("completed")),
        (["result", "run1"], {"run_id": "run1", "state": "completed", "steps": [], "kind": None}),
        (["runs"], []),
        (["status", "run1"], _snap("completed")),
    ):
        with (
            patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
            patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
        ):
            mock_req.get.return_value = _resp(200, body_or_rows)
            runner.invoke(workflow, argv)
        assert mock_req.get.call_args.kwargs["timeout"] == MCP_REQUEST_TIMEOUT, argv


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_happy(runner):
    body = {
        "run_id": "run1",
        "state": "running",
        "current_step_id": "s1",
        "steps": [{"id": "s1", "state": "running", "attempts": 1}],
    }
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["status", "run1"])
    assert result.exit_code == 0
    assert "running" in result.output


def test_status_unknown_404(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(404, {"detail": "unknown run 'ghost'"})
        result = runner.invoke(workflow, ["status", "ghost"])
    assert result.exit_code != 0
    assert "unknown run" in result.output


def test_status_no_id_resolves_most_recent(runner):
    """T12 (VR-4): ``status`` with no id resolves the most-recently-started run via
    ``?limit=1`` (first row) then GETs its snapshot."""
    rows = [
        {
            "run_id": "recent",
            "workflow_name": "wf",
            "state": "running",
            "tier": "yaml",
            "started_at": "2026-07-27T10:00:00Z",
            "finished_at": None,
            "current_step_id": "s1",
        },
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.side_effect = [
            _resp(200, rows),  # list ?limit=1
            _resp(200, _snap("running", run_id="recent", current="s1")),  # snapshot
        ]
        result = runner.invoke(workflow, ["status"])
    assert result.exit_code == 0
    assert "recent" in result.output
    # The list call clamped to a single most-recent row.
    first_call = mock_req.get.call_args_list[0]
    assert first_call.kwargs["params"] == {"limit": 1}


def test_status_no_id_empty_list(runner):
    """T12 (VR-4): ``status`` with no id and an empty run list prints "no runs
    found" and exits 0 (never a 404)."""
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, [])
        result = runner.invoke(workflow, ["status"])
    assert result.exit_code == 0
    assert "no runs found" in result.output


# ---------------------------------------------------------------------------
# runs (T11) — distinct from `list`
# ---------------------------------------------------------------------------
def test_runs_renders_table(runner):
    """T11 (VR-3): ``runs`` renders the RUN_ID/WORKFLOW/STATE/TIER/STARTED table."""
    rows = [
        {
            "run_id": "run1",
            "workflow_name": "wf",
            "state": "completed",
            "tier": "yaml",
            "started_at": "2026-07-27T10:00:00Z",
            "finished_at": "2026-07-27T10:05:00Z",
            "current_step_id": None,
        },
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["runs"])
    assert result.exit_code == 0
    assert "RUN_ID" in result.output and "WORKFLOW" in result.output
    assert "STATE" in result.output and "TIER" in result.output and "STARTED" in result.output
    assert "run1" in result.output and "completed" in result.output


def test_runs_json_and_filters(runner):
    """T11 (EC-3): ``runs --json --state --limit`` emits the array and forwards the
    filter params."""
    rows = [
        {
            "run_id": "run1",
            "workflow_name": "wf",
            "state": "failed",
            "tier": "yaml",
            "started_at": "t",
            "finished_at": None,
            "current_step_id": None,
        }
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["runs", "--state", "failed", "--limit", "5", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == rows
    _, kwargs = mock_req.get.call_args
    assert kwargs["params"] == {"state": "failed", "limit": 5}


def test_runs_empty(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, [])
        result = runner.invoke(workflow, ["runs"])
    assert result.exit_code == 0
    assert "No runs found" in result.output


def test_runs_illegal_state_400(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(400, {"detail": "illegal run state filter 'bogus'"})
        result = runner.invoke(workflow, ["runs", "--state", "bogus"])
    assert result.exit_code != 0
    assert "illegal run state" in result.output


def test_runs_help_distinct_from_list(runner):
    """T11 (VR-3): the ``runs`` help text is distinct from the ``list`` help text —
    ``runs`` lists RUNS, ``list`` lists SPECS."""
    runs_help = runner.invoke(workflow, ["runs", "--help"]).output
    list_help = runner.invoke(workflow, ["list", "--help"]).output
    assert runs_help != list_help
    assert "runs" in runs_help.lower()
    assert "indexed workflows" in list_help  # list's own help mentions the spec index


# ---------------------------------------------------------------------------
# wait (T9) — poll an existing run to terminal
# ---------------------------------------------------------------------------
def test_wait_polls_to_terminal_exit_0(runner):
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.get.side_effect = [_resp(200, _snap("running")), _resp(200, _snap("completed"))]
        result = runner.invoke(workflow, ["wait", "run1"])
    assert result.exit_code == 0
    assert "completed" in result.output


def test_wait_failed_exit_1(runner):
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        mock_req.get.return_value = _resp(200, _snap("cancelled"))
        result = runner.invoke(workflow, ["wait", "run1"])
    assert result.exit_code == 1


def test_wait_unknown_404(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(404, {"detail": "unknown run 'ghost'"})
        result = runner.invoke(workflow, ["wait", "ghost"])
    assert result.exit_code != 0
    assert "unknown run" in result.output


# ---------------------------------------------------------------------------
# result (T10) — retained run result
# ---------------------------------------------------------------------------
def test_result_happy(runner):
    body = {
        "run_id": "run1",
        "workflow_name": "wf",
        "state": "completed",
        "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
        "started_at": "t",
        "finished_at": "t2",
        "kind": None,
    }
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["result", "run1"])
    assert result.exit_code == 0
    assert "run1" in result.output and "completed" in result.output


def test_result_json_verbatim(runner):
    body = {
        "run_id": "run1",
        "workflow_name": "wf",
        "state": "failed",
        "steps": [],
        "started_at": "t",
        "finished_at": "t2",
        "kind": "error",
    }
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["result", "run1", "--json"])
    assert result.exit_code == 0
    # --json emits the server body verbatim (round-trippable).
    assert json.loads(result.stdout) == body


def test_result_unknown_404(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(404, {"detail": "unknown run 'ghost'"})
        result = runner.invoke(workflow, ["result", "ghost"])
    assert result.exit_code != 0
    assert "unknown run" in result.output


# ---------------------------------------------------------------------------
# U9 (issue #505): the failure envelope render on the human ``result`` output.
# ``--json`` stays the verbatim server body (NFR-3); the human render adds the
# envelope lines beneath a failed/cancelled result and omits them otherwise.
# ---------------------------------------------------------------------------
def _failed_result_body(run_id="run1", kind="error"):
    """A retained result body for a FAILED run carrying the U9 failure envelope."""
    return {
        "run_id": run_id,
        "workflow_name": "wf",
        "state": "failed",
        "steps": [{"id": "s2", "state": "failed", "attempts": 3}],
        "started_at": "t",
        "finished_at": "t2",
        "kind": kind,
        "failure_envelope": {
            "failing_step": "s2",
            "attempt": 3,
            "error_kind": kind,
            "terminal_reference": run_id,
            "next_command": f"cao workflow result {run_id}",
        },
    }


def test_result_renders_failure_envelope_for_failed_run(runner):
    """U9-T10 (FR-7.1): a failed run's human ``result`` render prints the failure
    envelope fields (failing step, attempt, error kind, terminal reference, next
    command)."""
    body = _failed_result_body()
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["result", "run1"])
    assert result.exit_code == 0
    out = result.output
    assert "Failure:" in out
    assert "s2" in out  # failing step
    assert "3" in out  # attempt
    assert "error" in out  # error kind
    assert "cao workflow result run1" in out  # next command hint


def test_result_failed_json_verbatim_carries_envelope(runner):
    """U9-T8 (ST-1 / NFR-3): ``result --json`` for a failed run emits the server
    body verbatim — the failure envelope (and its stable next_command hint) is in
    the JSON, round-trippable, not re-shaped by the CLI."""
    body = _failed_result_body(kind="timeout")
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["result", "run1", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == body
    assert parsed["failure_envelope"]["next_command"] == "cao workflow result run1"


def test_result_completed_run_renders_no_failure_block(runner):
    """U9 (NFR-3): a COMPLETED run carries no envelope, so the human render shows no
    ``Failure:`` block — the success render is unchanged."""
    body = {
        "run_id": "run1",
        "workflow_name": "wf",
        "state": "completed",
        "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
        "started_at": "t",
        "finished_at": "t2",
        "kind": None,
    }
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["result", "run1"])
    assert result.exit_code == 0
    assert "Failure:" not in result.output


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
def test_cancel_happy(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, {"success": True, "run_id": "run1"})
        result = runner.invoke(workflow, ["cancel", "run1"])
    assert result.exit_code == 0
    assert "cancelling" in result.output
    # cancel is a quick write — it correctly keeps the flat MCP_REQUEST_TIMEOUT.
    _, kwargs = mock_req.post.call_args
    assert kwargs["timeout"] == MCP_REQUEST_TIMEOUT


def test_cancel_finished_409(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(409, {"detail": "run 'run1' is already completed"})
        result = runner.invoke(workflow, ["cancel", "run1"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# list (Bug 1: a script-tier row's step_count=None must not crash the table)
# ---------------------------------------------------------------------------
def test_list_renders_none_step_count_as_dash(runner):
    """A script spec indexes with step_count=None (run-time-determined). The table
    must render that as '-', never crash formatting None with the :<6 field."""
    rows = [
        {"name": "yamlwf", "mode": "sequential", "step_count": 3, "description": "a yaml one"},
        {"name": "scriptwf", "mode": "script", "step_count": None, "description": "a script one"},
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["list"])
    assert result.exit_code == 0
    # The YAML row shows its numeric count; the script row shows the placeholder.
    # Assert on the rendered data lines specifically — the header underline is a
    # run of dashes, so a bare "'-' in output" would pass even without the fix.
    lines = result.output.splitlines()
    yaml_line = next(line for line in lines if line.startswith("yamlwf"))
    script_line = next(line for line in lines if line.startswith("scriptwf"))
    assert "3" in yaml_line.split()  # YAML row's numeric step count
    assert "-" in script_line.split()  # script row renders None as the placeholder
    assert "None" not in script_line  # never the literal None


def test_list_all_rows_script_none_step_count(runner):
    """Edge case: a listing of ONLY script specs (every step_count None) still
    renders every row without a TypeError."""
    rows = [
        {"name": "s1", "mode": "script", "step_count": None, "description": ""},
        {"name": "s2", "mode": "script", "step_count": None, "description": "second"},
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["list"])
    assert result.exit_code == 0
    assert "s1" in result.output and "s2" in result.output


def test_list_empty(runner):
    """Edge case: an empty index prints a friendly message, not an empty table."""
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, [])
        result = runner.invoke(workflow, ["list"])
    assert result.exit_code == 0
    assert "No workflows found" in result.output


def test_list_json_passthrough_preserves_none(runner):
    """The --json path emits rows verbatim (step_count stays null), never coerced."""
    rows = [{"name": "scriptwf", "mode": "script", "step_count": None, "description": ""}]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["list", "--json"])
    assert result.exit_code == 0
    assert '"step_count": null' in result.output
