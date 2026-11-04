"""#735 guard: the turn-identity gate must not be fail-open on a dropped field.

#566 built the recency token that says WHICH turn a status belongs to
(``StatusMonitor.output_generation`` / ``status_observation``), but it is
*opt-in per call site* and *fail-open on a missing field*. Both halves bite:

1. A dispatch-then-wait site that never asks for the boundary gets the pre-#566
   contract and is called done by the previous turn's latched COMPLETED. That was
   #735 on ``cao session send``, and before it #407. The call-site half is owned
   by rule 3 of ``test/test_callsite_lint_rules.py`` (the staleness gate, #1011):
   every ``poll_until_done`` call passes ``min_generation=`` or carries a
   ``# staleness-ok: <reason>`` marker at the site. PR #999 briefly carried a
   second lint here with its own ``turn-boundary: ungated`` marker; it was
   retired so that there is one rule and one marker. Two waits that rule does
   not see are recorded, not guarded: ``services/agent_step.py``'s
   ``_wait_for_completion`` after ``send_input`` (a live sibling of #735, filed
   separately; the comment at the site says so) and
   ``services/memory_service.py``'s reused-curator path, which polls with an
   inline ``for _ in range(30)`` loop and is bounded (not fixed) by the
   ``output_before`` watermark above it.
2. The gate degrades deliberately to the old frame-based wait when the reading
   carries no generation (older cao-server, event-inbox backends). So if any link
   in ``get_terminal`` -> ``Terminal`` -> ``GET /terminals/{id}`` ->
   ``poll_until_done`` stops carrying the field, the gate silently switches off
   and every per-side test keeps passing: each asserts its own literal key name.

The test below closes the second half structurally: it deliberately never names
the wire field, so it cannot be satisfied by a rename on both sides of one seam.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import click
import pytest

from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus


@pytest.fixture
def _finished_turn():
    """A status monitor sitting on a COMPLETED earned before the next send."""
    monitor = MagicMock()
    observation = MagicMock()
    observation.status = TerminalStatus.COMPLETED
    observation.output_generation = 8
    monitor.status_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.COMPLETED
    # Nothing has landed since the send: the gate's unbounded state (P2-2). A bare
    # MagicMock here coerces to int 1 through the Terminal model and reads as a
    # server restart instead.
    monitor.output_generation.return_value = 8
    return monitor


def test_the_status_a_client_reads_still_carries_which_turn_earned_it(_finished_turn):
    """The gate must not be switchable off by dropping a field on the way out.

    Builds the GET body the way production does -- ``terminal_service.get_terminal``
    through the real ``Terminal`` model -- and feeds it to the real
    ``poll_until_done`` with a boundary. This test never names the field that
    carries the generation, so a rename or a drop anywhere along that chain makes
    the gate degrade to the pre-#735 wait and fails here, where the per-side tests
    (each asserting its own literal key) all still pass.
    """
    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.utils.terminal import poll_until_done

    with (
        patch.object(terminal_service, "status_monitor", _finished_turn),
        patch.object(
            terminal_service,
            "get_terminal_metadata",
            return_value={
                "id": "term735",
                "tmux_window": "developer-term735",
                "tmux_session": "s-735",
                "provider": "claude_code",
                "agent_profile": "developer",
                "last_active": datetime.now(),
            },
        ),
        patch.object(terminal_service, "get_deferred_init_failure", return_value=None),
        patch.object(terminal_service, "callback_terminal_id_for_session", return_value=None),
    ):
        body = Terminal(**terminal_service.get_terminal("term735")).model_dump()

    assert body["status"] == TerminalStatus.COMPLETED.value, "fixture no longer reads COMPLETED"
    # The pair must come from ONE lock read. Two separate reads (get_status(),
    # then a generation accessor) let a detector apply this turn's PROCESSING in
    # between and pair the previous turn's COMPLETED with the new turn's stamp,
    # which is a reading the gate below would wrongly accept (review P3-2; the
    # separate accessor was removed so no caller can pair them again).
    _finished_turn.status_observation.assert_called_once()
    _finished_turn.get_status.assert_not_called()

    reads = []

    def get(url, timeout=None):
        reads.append(url)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = body
        return response

    # An ungated wait returns on this COMPLETED at the first read. The gate, fed
    # the body above, must refuse it for the whole (short) timeout instead: that
    # raise IS the assertion, so it is spelled out rather than left to
    # ``pytest.raises`` with a read count beside it (review P3-1).
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        try:
            poll_until_done("term735", timeout=0.05, polling_interval=0.01, min_generation=8)
        except click.ClickException as exc:
            assert "Timed out" in exc.message, exc.message
        else:
            pytest.fail(
                "GET /terminals/{id}'s body no longer tells poll_until_done which turn "
                "the status was earned from, so the #735 gate silently degraded to the "
                "frame-based wait and finished on the previous turn's COMPLETED after "
                f"{len(reads)} read(s)"
            )
