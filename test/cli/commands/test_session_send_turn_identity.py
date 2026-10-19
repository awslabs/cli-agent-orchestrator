"""#735: `cao session send` must not call a turn done on the previous turn's frame.

`cao session send` posts the message, sleeps a flat three seconds, then polls
``GET /terminals/{id}`` -- a status that describes the terminal's *current frame*.
Nothing in that exchange names the turn that was sent:

- ``POST /terminals/{id}/input`` answers ``{"success": true}``; the dispatch
  boundary ``terminal_service.dispatch_input`` already computes is thrown away by
  the ``send_input`` wrapper (terminal_service.py:3853).
- ``GET /terminals/{id}`` reports ``status`` with no indication of which output it
  was earned from, even though ``StatusMonitor.status_observation`` can supply
  exactly that pair (status_monitor.py:903).
- ``poll_until_done`` returns on the first COMPLETED it sees (utils/terminal.py:301).

``notify_input_sent`` deliberately leaves ``_last_status`` alone (status_monitor.py:936,
and see the ``output_generation`` docstring at :858), so for the whole window between a
dispatch and the new turn's first applied PROCESSING the server answers the CLI's poll
with the PREVIOUS turn's COMPLETED. These tests drive the real ``StatusMonitor`` through
that window and assert that the CLI's own wait logic does not treat it as done.
"""

from unittest.mock import MagicMock, patch

import click
import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor
from cli_agent_orchestrator.utils.terminal import poll_until_done

TERMINAL_ID = "term-735"


def _tmux_backend():
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    return backend


def _latch(sm, status, generation):
    """Apply a status the way the pipeline does, without the DB write.

    ``_apply_detection`` persists to the terminals table; these tests exercise the
    in-memory latch and the generation stamp only, so they set the same fields the
    locked core would (status_monitor.py:459, :490).
    """
    sm._output_generation[TERMINAL_ID] = generation
    with sm._lock:
        sm._last_status[TERMINAL_ID] = status
        sm._status_generation[TERMINAL_ID] = generation
        sm._idle_from_completed.pop(TERMINAL_ID, None)
        if status == TerminalStatus.PROCESSING:
            sm._allow_processing_revert[TERMINAL_ID] = False


@pytest.fixture
def monitor():
    """A real StatusMonitor sitting on a finished turn, as after a previous send."""
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend") as get_backend,
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=None,
        ),
    ):
        get_backend.return_value = _tmux_backend()
        sm = StatusMonitor()
        # Previous turn ran and finished: its output landed, then COMPLETED latched.
        _latch(sm, TerminalStatus.PROCESSING, 7)
        _latch(sm, TerminalStatus.COMPLETED, 8)
        assert sm.get_status(TERMINAL_ID) == TerminalStatus.COMPLETED
        yield sm


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_status_after_dispatch_is_the_previous_turns_completed(get_backend, monitor):
    """Pins the server-side half: the poll the CLI makes 3s after a send can only
    be answered with the previous turn's COMPLETED until PROCESSING is applied."""
    get_backend.return_value = _tmux_backend()

    boundary = monitor.output_generation(TERMINAL_ID)
    monitor.notify_input_sent(TERMINAL_ID)  # exactly what dispatch_input does

    # No output for this turn has landed yet, and the status still reads ready.
    assert monitor.get_status(TERMINAL_ID) == TerminalStatus.COMPLETED
    observation = monitor.status_observation(TERMINAL_ID)
    assert observation.status == TerminalStatus.COMPLETED
    # ... and it was earned at or before the dispatch boundary, so it is NOT this
    # turn's completion. The information needed to reject it exists; GET
    # /terminals/{id} just never puts it on the wire.
    assert observation.output_generation is not None
    assert observation.output_generation <= boundary


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_poll_until_done_returns_on_the_previous_turns_completion(get_backend, monitor):
    """#735: the CLI's wait returns immediately on a pre-dispatch COMPLETED.

    ``read_status`` is the real read path's shape -- it returns exactly the string
    ``GET /terminals/{id}`` puts in ``status``, sourced here from a real
    StatusMonitor in the post-dispatch window. Fails before the fix (returns with
    the turn unstarted); after the fix it must keep waiting.
    """
    get_backend.return_value = _tmux_backend()
    monitor.notify_input_sent(TERMINAL_ID)

    reads = []

    def read_status(terminal_id):
        reads.append(terminal_id)
        if len(reads) <= 3:
            # The window: previous turn's COMPLETED is still latched.
            return monitor.get_status(terminal_id).value
        # The turn finally starts, then genuinely finishes.
        if len(reads) == 4:
            _latch(monitor, TerminalStatus.PROCESSING, 9)
        return monitor.get_status(terminal_id).value

    poll_until_done(TERMINAL_ID, timeout=30.0, polling_interval=0.01, read_status=read_status)

    assert len(reads) > 3, (
        "poll_until_done accepted the previous turn's COMPLETED as this turn's "
        f"completion after {len(reads)} read(s); `cao session send` then prints "
        "whatever the buffer holds (#735)"
    )


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_poll_until_done_still_returns_on_a_genuine_completion(get_backend, monitor):
    """Guard against over-correcting: a COMPLETED earned after the dispatch
    boundary must still end the wait promptly."""
    get_backend.return_value = _tmux_backend()
    monitor.notify_input_sent(TERMINAL_ID)

    reads = []

    def read_status(terminal_id):
        reads.append(terminal_id)
        if len(reads) == 1:
            _latch(monitor, TerminalStatus.PROCESSING, 9)
        elif len(reads) == 2:
            _latch(monitor, TerminalStatus.COMPLETED, 10)
        return monitor.get_status(terminal_id).value

    poll_until_done(TERMINAL_ID, timeout=30.0, polling_interval=0.01, read_status=read_status)

    assert len(reads) <= 4


def test_input_endpoint_contract_hides_the_dispatch_boundary():
    """``send_input`` collapses ``dispatch_input``'s boundary to ``True``, so the
    HTTP caller cannot name the turn it just started (terminal_service.py:3853)."""
    import cli_agent_orchestrator.services.terminal_service as ts

    with patch.object(ts, "dispatch_input", return_value=42) as dispatch:
        assert ts.send_input(TERMINAL_ID, "hello") is True
    assert dispatch.called
