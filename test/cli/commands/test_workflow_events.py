"""Tests for the U10 live-event follower CLI verb (issue #505, FR-4.9).

``cao workflow events <run-id> --follow`` is a CLIENT-SIDE consumer of #504's
events-follow SSE route. #504's server route is NOT in this tree yet (HEAD is the
base commit); every test here STUBS the streamed SSE response by feeding
``requests.get`` a mock whose ``iter_lines`` replays the FINAL frame contract's
lines. Marker: ``integration`` (NOT e2e — CI runs ``-m 'not e2e'`` and an e2e
marker would make this guard silently absent).

The LOAD-BEARING pair is ``test_gap_rendered_as_declared`` +
``test_no_gap_frame_no_synthesized_gap``: together they prove the follower RENDERS
the server-declared ``event: gap`` frame and does NOT infer a gap from a seq jump
(GD-1, render-not-infer). If the follower ever computed gaps from numbering, the
no-declaration test would fail.

``requests`` is mocked — no server. black + isort (line 100).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import workflow
from cli_agent_orchestrator.constants import WORKFLOW_EVENTS_MAX_RECONNECTS

pytestmark = pytest.mark.integration

# CliRunner's stdout is NOT a TTY (and click swaps sys.stdout during invoke, so
# patching isatty is unreliable), so the follower defaults to MACHINE mode (a JSONL
# stream). The human-render assertions below force interactive mode by patching
# ``_machine_mode`` -> False, so the human progress lines ("[seq N] ...", the
# "⚠ gap ..." notice, "stream ended ...") are emitted.
_HUMAN = patch("cli_agent_orchestrator.cli.commands.workflow._machine_mode", return_value=False)


@pytest.fixture
def runner():
    return CliRunner()


def _sse_lines(*frames: str):
    """Flatten SSE frame blocks into the decoded line list ``iter_lines`` yields.

    Each ``frame`` is a full SSE block (its own ``event:``/``data:``/``id:`` lines
    plus the terminating blank line), authored exactly as #504 puts them on the
    wire. ``iter_lines(decode_unicode=True)`` yields each line WITHOUT the trailing
    newline, so we split on ``\\n`` and keep the empty strings (the blank line that
    terminates a frame).
    """
    lines: list[str] = []
    for frame in frames:
        lines.extend(frame.split("\n"))
    return lines


def _event_frame(seq, event_type, step_id, state, ts="2026-07-28T00:00:00Z"):
    """A NORMAL event frame: event / data(full EventRow) / id / blank line."""
    data = {
        "seq": seq,
        "run_id": "run1",
        "event_type": event_type,
        "step_id": step_id,
        "state": state,
        "ts": ts,
    }
    return f"event: {event_type}\ndata: {json.dumps(data)}\nid: {seq}\n"


def _gap_frame(after_seq, before_seq, missing_count, reason="append_failed"):
    """A DECLARED gap frame: event: gap / data(range) / blank line. NO id: line."""
    data = {
        "after_seq": after_seq,
        "before_seq": before_seq,
        "missing_count": missing_count,
        "reason": reason,
    }
    return f"event: gap\ndata: {json.dumps(data)}\n"


def _stream_resp(*frames: str, status_code=200):
    """A mock streamed SSE response whose ``iter_lines`` replays the frames."""
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.iter_lines.return_value = iter(_sse_lines(*frames))
    r.close.return_value = None
    return r


def _snap_resp(state, status_code=200):
    """A mock snapshot response from ``GET /workflows/runs/{id}`` (status fallback)."""
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.json.return_value = {"run_id": "run1", "state": state}
    return r


# ---------------------------------------------------------------------------
# OR-1: renders normal frames in seq order and OR-2: closes on terminal.
# ---------------------------------------------------------------------------
def test_renders_normal_frames_in_seq_order(runner):
    """OR-1/OR-2: normal frames render in ascending seq; a terminal run.* frame
    ends the follow and yields exit 0 for a completed run."""
    stream = _stream_resp(
        _event_frame(1, "step.completed", "s1", "completed"),
        _event_frame(2, "step.completed", "s2", "completed"),
        _event_frame(3, "run.completed", None, "completed"),
    )
    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream
        ) as get,
        _HUMAN,
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    # Rendered in seq order.
    out = result.output
    assert out.index("seq 1") < out.index("seq 2") < out.index("seq 3")
    assert "run.completed" in out
    # Opened the SSE variant of the events route.
    args, kwargs = get.call_args
    assert args[0].endswith("/workflows/runs/run1/events")
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert kwargs["stream"] is True


def test_terminal_failed_frame_exits_1(runner):
    """OR-2 + EC-1: a run.failed terminal frame yields a non-zero exit."""
    stream = _stream_resp(
        _event_frame(1, "step.attempt.failed", "s1", "failed"),
        _event_frame(2, "run.failed", None, "failed"),
    )
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 1


def test_step_completed_state_does_not_close_follow_early(runner):
    """A step.completed frame carries state=completed (the STEP's state); it must
    NOT be mistaken for a terminal run state. The follow continues to run.failed."""
    stream = _stream_resp(
        _event_frame(1, "step.completed", "s1", "completed"),
        _event_frame(2, "run.failed", None, "failed"),
    )
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream),
        _HUMAN,
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    # Closed on the RUN terminal (failed), not the step's completed -> exit 1.
    assert result.exit_code == 1
    assert "seq 2" in result.output


# ---------------------------------------------------------------------------
# GD-1 (LOAD-BEARING PAIR): render the DECLARED gap; never infer from numbering.
# ---------------------------------------------------------------------------
def test_gap_rendered_as_declared(runner):
    """GD-1/GD-2: a server-DECLARED ``event: gap`` frame is rendered verbatim (the
    missing range + reason the FRAME carries), even though the surrounding seqs
    (19, 23) jump. The gap comes from the frame, not from arithmetic."""
    stream = _stream_resp(
        _event_frame(19, "step.completed", "s1", "completed"),
        _gap_frame(after_seq=19, before_seq=23, missing_count=3),
        _event_frame(23, "run.completed", None, "completed"),
    )
    with (
        patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream),
        _HUMAN,
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    out = result.output
    assert "gap" in out
    # The declared numbers appear exactly as the frame declared them.
    assert "3 event(s) lost" in out
    assert "seq 19" in out and "23" in out
    assert "append_failed" in out


def test_no_gap_frame_no_synthesized_gap(runner):
    """GD-1 (render-not-infer, MANDATED): the SAME seq jump 19 -> 23 with NO declared
    gap frame must NOT be reported as a gap. This FAILS if the follower infers gaps
    from numbering instead of consuming the ``event: gap`` frame."""
    stream = _stream_resp(
        _event_frame(19, "step.completed", "s1", "completed"),
        # seq jumps to 23 with NO gap frame between them.
        _event_frame(23, "run.completed", None, "completed"),
    )
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    # The follower renders what the server DECLARES; a bare seq jump is not a gap.
    assert "gap" not in result.output
    assert "lost" not in result.output


def test_gap_frame_does_not_advance_cursor_or_close(runner):
    """A gap frame carries no id: and is never terminal — it neither advances the
    resume cursor nor ends the follow; the stream continues to its terminal."""
    stream = _stream_resp(
        _event_frame(10, "step.completed", "s1", "completed"),
        _gap_frame(after_seq=10, before_seq=12, missing_count=1),
        _event_frame(12, "run.cancelled", None, "cancelled"),
    )
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1"])
    # Cancelled is terminal-non-completed -> exit 1; the gap did not end the follow.
    assert result.exit_code == 1
    assert "run.cancelled" in result.output


# ---------------------------------------------------------------------------
# RS-1/RS-3: reconnect resumes exactly via ?after_seq = last-seen id.
# ---------------------------------------------------------------------------
def test_reconnect_uses_last_id_as_after_seq(runner):
    """RS-1/RS-3: a dropped connection reconnects with ``?after_seq=<last seq>`` (and
    Last-Event-ID mirrored), so resume is exact/dedupe-free. First stream yields two
    frames then RAISES a transport error; the reconnect must resume after seq 2."""
    first = MagicMock(spec=requests.Response)
    first.status_code = 200

    def _raise_after_two():
        yield from _sse_lines(
            _event_frame(1, "step.completed", "s1", "completed"),
            _event_frame(2, "step.completed", "s2", "completed"),
        )
        raise requests.exceptions.ConnectionError("dropped")

    first.iter_lines.return_value = _raise_after_two()
    first.close.return_value = None
    second = _stream_resp(_event_frame(3, "run.completed", None, "completed"))

    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=[first, second],
        ) as get,
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    # Two GETs: the initial open + the reconnect.
    assert get.call_count == 2
    reconnect_kwargs = get.call_args_list[1].kwargs
    assert reconnect_kwargs["params"]["after_seq"] == 2
    assert reconnect_kwargs["headers"]["Last-Event-ID"] == "2"


def test_after_seq_flag_sets_initial_cursor(runner):
    """--after-seq seeds the initial resume cursor on the very first open."""
    stream = _stream_resp(_event_frame(6, "run.completed", None, "completed"))
    with patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream
    ) as get:
        result = runner.invoke(workflow, ["events", "run1", "--after-seq", "5"])
    assert result.exit_code == 0
    assert get.call_args.kwargs["params"]["after_seq"] == 5


# ---------------------------------------------------------------------------
# F-1 terminal guard: a stream that ends WITHOUT a terminal event must not hang.
# ---------------------------------------------------------------------------
def test_stream_ends_without_terminal_does_final_status_check(runner):
    """F-1: the stream ends with NO terminal frame (a swallowed terminal event). The
    follower must NOT hang — it does a final GET status and closes on the terminal
    state read there (here: completed -> exit 0)."""
    stream = _stream_resp(_event_frame(1, "step.completed", "s1", "completed"))
    with patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        side_effect=[stream, _snap_resp("completed")],
    ) as get:
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    # The 2nd GET is the snapshot route (the terminal guard).
    assert get.call_args_list[1].args[0].endswith("/workflows/runs/run1")
    assert "completed" in result.output


def test_stream_ends_non_terminal_reports_stream_ended_exit_0(runner):
    """F-1: stream ends, and the final status shows the run is NOT terminal — report
    the stream ended and exit 0 (a lost stream is never a false failure)."""
    stream = _stream_resp(_event_frame(1, "step.completed", "s1", "completed"))
    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=[stream, _snap_resp("running")],
        ),
        _HUMAN,
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    assert "stream ended" in result.output


def test_reconnect_budget_exhausted_falls_back_to_status(runner):
    """A persistently flapping stream cannot spin forever: after the reconnect
    budget (WORKFLOW_EVENTS_MAX_RECONNECTS) is spent the follower drops to a final
    status check and closes. The budget bounds re-opens to MAX+1 stream GETs, then
    one final status GET resolves it terminal."""
    dropping = requests.exceptions.ConnectionError("dropped")
    drops = [dropping] * (WORKFLOW_EVENTS_MAX_RECONNECTS + 1)
    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=drops + [_snap_resp("completed")],
        ),
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep"),
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    assert "completed" in result.output


# ---------------------------------------------------------------------------
# CC-1: Ctrl-C detaches (exit 0, hint, no cancel POST).
# ---------------------------------------------------------------------------
def test_ctrl_c_detaches_without_cancel(runner):
    """CC-1: a KeyboardInterrupt mid-follow DETACHES — exit 0, a "still running"
    hint, and NO cancel POST is issued."""
    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=KeyboardInterrupt,
        ),
        patch("cli_agent_orchestrator.cli.commands.workflow.requests.post") as post,
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    assert "still running" in result.output
    assert "run1" in result.output
    post.assert_not_called()


# ---------------------------------------------------------------------------
# --json / non-TTY: each frame is a stable machine JSON line (gap included).
# ---------------------------------------------------------------------------
def test_json_emits_stable_lines_including_gap(runner):
    """--json: each frame is a stable JSON line — a gap frame as {"kind":"gap",...},
    a normal frame as {"kind":"event",...}, and the terminal as {"kind":"terminal"}.
    The terminal exit code is preserved."""
    stream = _stream_resp(
        _event_frame(19, "step.completed", "s1", "completed"),
        _gap_frame(after_seq=19, before_seq=21, missing_count=1),
        _event_frame(21, "run.completed", None, "completed"),
    )
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1", "--json"])
    assert result.exit_code == 0
    lines = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip()]
    kinds = [obj["kind"] for obj in lines]
    assert kinds == ["event", "gap", "event", "terminal"]
    gap = next(o for o in lines if o["kind"] == "gap")
    assert gap == {
        "kind": "gap",
        "after_seq": 19,
        "before_seq": 21,
        "missing_count": 1,
        "reason": "append_failed",
    }
    terminal = lines[-1]
    assert terminal == {"kind": "terminal", "run_id": "run1", "state": "completed"}


def test_non_tty_json_no_gap_frame_still_no_synthesized_gap(runner):
    """GD-1 in machine mode: a seq jump with NO gap frame emits NO gap line under
    --json either (render-not-infer holds regardless of output mode)."""
    stream = _stream_resp(
        _event_frame(19, "step.completed", "s1", "completed"),
        _event_frame(23, "run.completed", None, "completed"),
    )
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1", "--json"])
    assert result.exit_code == 0
    lines = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip()]
    assert not any(o["kind"] == "gap" for o in lines)


# ---------------------------------------------------------------------------
# error surfacing + the --no-follow batch read + the thin-client boundary.
# ---------------------------------------------------------------------------
def test_unknown_run_404(runner):
    """A 404 on BOTH the events route and the snapshot probe means the RUN is
    genuinely unknown — the message must stay run-scoped."""
    stream = _stream_resp(status_code=404)
    stream.json.return_value = {"detail": "unknown run 'ghost'"}
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "ghost"])
    assert result.exit_code != 0
    assert "unknown run" in result.output


def test_absent_events_route_reported_as_capability_not_unknown_run(runner):
    """CD-1: a 404 from the events route on a run that IS readable means the ROUTE is
    missing (it ships with issue #504), not the run.

    Reporting "unknown run" for a perfectly healthy run sends the operator hunting a
    nonexistent problem. The snapshot route discriminates: 200 there proves the run
    exists, so the 404 came from the absent route, and the message must say so and
    name a working alternative.

    MUTATION PROOF: revert the 404 arm to a bare
    ``raise click.ClickException(f"unknown run '{run_id}'")`` and this goes RED.
    """
    events_404 = _stream_resp(status_code=404)
    events_404.json.return_value = {"detail": "Not Found"}
    snapshot_200 = MagicMock()
    snapshot_200.status_code = 200
    snapshot_200.json.return_value = {"run_id": "live-1", "state": "running", "steps": []}

    with patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        side_effect=[events_404, snapshot_200],
    ):
        result = runner.invoke(workflow, ["events", "live-1"])
    assert result.exit_code != 0
    # Names the CAPABILITY, not the run, and points at something that works.
    assert "no live event stream" in result.output
    assert "unknown run" not in result.output
    assert "cao workflow wait live-1" in result.output


def test_absent_events_route_on_batch_read_also_degrades(runner):
    """CD-1: ``--no-follow`` reads the SAME route, so it is equally absent and must
    give the same capability-scoped message rather than "unknown run"."""
    batch_404 = MagicMock()
    batch_404.status_code = 404
    batch_404.json.return_value = {"detail": "Not Found"}
    snapshot_200 = MagicMock()
    snapshot_200.status_code = 200
    snapshot_200.json.return_value = {"run_id": "live-2", "state": "running", "steps": []}

    with patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        side_effect=[batch_404, snapshot_200],
    ):
        result = runner.invoke(workflow, ["events", "live-2", "--no-follow"])
    assert result.exit_code != 0
    assert "no live event stream" in result.output
    assert "unknown run" not in result.output


def test_probe_transport_failure_falls_back_to_run_scoped_message(runner):
    """CD-1 conservatism: if the discriminating probe itself fails, do NOT assert a
    server capability that could not be verified — fall back to the run-scoped
    message rather than blaming a missing route on no evidence."""
    events_404 = _stream_resp(status_code=404)
    events_404.json.return_value = {"detail": "unknown run 'maybe'"}

    with patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get",
        side_effect=[events_404, requests.ConnectionError("probe down")],
    ):
        result = runner.invoke(workflow, ["events", "maybe"])
    assert result.exit_code != 0
    assert "unknown run" in result.output
    assert "no live event stream" not in result.output


def test_no_follow_batch_read(runner):
    """--no-follow does a one-shot batch (JSON-variant) read of the same route."""
    rows = [
        {
            "seq": 1,
            "run_id": "run1",
            "event_type": "step.completed",
            "step_id": "s1",
            "state": "completed",
            "ts": "t",
        },
        {
            "seq": 2,
            "run_id": "run1",
            "event_type": "run.completed",
            "step_id": None,
            "state": "completed",
            "ts": "t",
        },
    ]
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.json.return_value = rows
    with patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=resp
    ) as get:
        result = runner.invoke(workflow, ["events", "run1", "--no-follow"])
    assert result.exit_code == 0
    assert "seq 1" in result.output and "seq 2" in result.output
    # Batch read does NOT request the SSE variant.
    assert "headers" not in get.call_args.kwargs or "Accept" not in get.call_args.kwargs.get(
        "headers", {}
    )


def test_cli_module_is_thin_http_client_no_engine_import():
    """C-2 boundary: the workflow CLI module IMPORTS no engine/journal/DAL (FR-7.4 /
    the project Forbidden rule). AST-scans the real import statements — not the
    source text, whose module docstring names the forbidden symbols on purpose."""
    import ast
    import inspect

    import cli_agent_orchestrator.cli.commands.workflow as mod

    tree = ast.parse(inspect.getsource(mod))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            targets.append(base)
            targets.extend(f"{base}.{a.name}" if base else a.name for a in node.names)

    forbidden_substrings = (
        "workflow_service",
        "workflow_journal",
        "script_runner",
        "clients.database",
        "clients.tmux",
        "event_log",
    )
    violations = [t for t in targets if any(bad in t for bad in forbidden_substrings)]
    assert not violations, f"thin client must not import {violations}"


# ---------------------------------------------------------------------------
# FD-1 (PR #525 review): the streamed response is CLOSED on every exit path.
#
# ``stream=True`` holds the connection open until it is explicitly closed or fully
# drained, and this generator is routinely abandoned WITHOUT draining: the follow
# loop ``break``s the instant a terminal frame arrives, and each reconnect leaves the
# previous generator suspended. Before the fix the socket/FD survived until GC, so a
# long follow with repeated reconnects accumulated live sockets. The MCP twin
# ``workflow_events`` was hardened for exactly this; the CLI twin was not.
# ---------------------------------------------------------------------------
def test_stream_closed_after_terminal_frame_break(runner):
    """The terminal-frame ``break`` path closes the response.

    This is the common case and the one a naive ``try``/``finally``-less
    implementation leaks on every single successful follow.

    MUTATION PROOF: remove the ``try``/``finally`` from ``_stream_event_frames`` and
    ``close`` is never called, failing the assertion.
    """
    stream = _stream_resp(
        _event_frame(1, "step.completed", "s1", "completed"),
        _event_frame(2, "run.completed", None, "completed"),
        # Trailing frames the follower will NEVER read: it breaks on the terminal
        # frame above, so the generator is abandoned mid-stream and undrained.
        _event_frame(3, "step.completed", "s3", "completed"),
        _event_frame(4, "step.completed", "s4", "completed"),
    )
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    stream.close.assert_called()


def test_stream_closed_on_non_200_error_arm(runner):
    """The non-200 arm closes too — an early ``raise`` must not leak the socket."""
    stream = _stream_resp(status_code=500)
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code != 0
    stream.close.assert_called()


def test_every_reconnect_closes_its_own_stream(runner):
    """Each reconnect closes the stream it abandoned, so a flapping follow cannot
    accumulate live sockets across attempts."""
    dropped = MagicMock(spec=requests.Response)
    dropped.status_code = 200
    dropped.close.return_value = None

    def _drop_midway():
        yield from _sse_lines(_event_frame(1, "step.completed", "s1", "completed"))
        raise requests.exceptions.ConnectionError("socket died")

    dropped.iter_lines.return_value = _drop_midway()
    final = _stream_resp(_event_frame(2, "run.completed", None, "completed"))

    with (
        patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get",
            side_effect=[dropped, final],
        ),
        patch("cli_agent_orchestrator.cli.commands.workflow.time.sleep", lambda *_: None),
    ):
        result = runner.invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    # BOTH the dropped stream and the reconnected one were closed.
    dropped.close.assert_called()
    final.close.assert_called()
