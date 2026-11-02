"""#735: `cao session send` must not call a turn done on the previous turn's frame.

`cao session send` posts the message, sleeps a flat three seconds, then polls
``GET /terminals/{id}`` -- a status that describes the terminal's *current frame*.
Nothing in that exchange names the turn that was sent:

- ``POST /terminals/{id}/input`` answers ``{"success": true}``; the dispatch
  boundary ``terminal_service.dispatch_input`` already computes is thrown away by
  the ``send_input`` wrapper.
- ``GET /terminals/{id}`` reports ``status`` with no indication of which output it
  was earned from, even though ``StatusMonitor.status_observation`` can supply
  exactly that pair.
- ``poll_until_done`` returned on the first COMPLETED it saw, whatever turn it
  belonged to.

``notify_input_sent`` deliberately leaves ``_last_status`` alone (see it and the
``output_generation`` docstring in status_monitor.py), so for the whole window between a
dispatch and the new turn's first applied PROCESSING the server answers the CLI's poll
with the PREVIOUS turn's COMPLETED. These tests drive the real ``StatusMonitor`` through
that window and assert that the CLI's own wait logic does not treat it as done.
"""

import time
from unittest.mock import MagicMock, patch

import click
import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor
from cli_agent_orchestrator.utils import api_http
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
    locked core (``_apply_detection_locked``) would.
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


def _http_reader(monitor, boundary, script):
    """A fake ``GET /terminals/{id}`` sourced from a real StatusMonitor.

    Reads the same three things ``terminal_service.get_terminal`` puts on the wire,
    in the same order: the (status, stamp) pair in one lock, then the current
    output counter. ``script`` is called with the 1-based read number and may
    advance the monitor before the reading is taken, so a test describes a turn's
    timeline rather than hand-rolling status payloads.
    """
    reads = []

    def get(url, timeout=None):
        reads.append(url)
        script(len(reads))
        observation = monitor.status_observation(TERMINAL_ID)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": observation.status.value,
            "status_generation": observation.output_generation,
            "output_generation": monitor.output_generation(TERMINAL_ID),
        }
        return response

    return reads, get


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_poll_until_done_refuses_the_previous_turns_completion(get_backend, monitor):
    """#735: the wait must not end on a COMPLETED earned before the dispatch.

    Before the fix ``poll_until_done`` returned on the first read, with the turn
    not yet started, and ``cao session send`` printed whatever the buffer held.
    """
    get_backend.return_value = _tmux_backend()
    boundary = monitor.output_generation(TERMINAL_ID)
    monitor.notify_input_sent(TERMINAL_ID)

    def script(n):
        # Reads 1-3 fall in the window where the previous turn's COMPLETED is
        # still latched; the turn then starts and genuinely finishes.
        if n == 4:
            _latch(monitor, TerminalStatus.PROCESSING, 9)
        elif n == 6:
            _latch(monitor, TerminalStatus.COMPLETED, 10)

    reads, get = _http_reader(monitor, boundary, script)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        poll_until_done(TERMINAL_ID, timeout=30.0, polling_interval=0.01, min_generation=boundary)

    assert len(reads) >= 6, (
        "poll_until_done accepted the previous turn's COMPLETED as this turn's "
        f"completion after {len(reads)} read(s); `cao session send` then prints "
        "whatever the buffer holds (#735)"
    )


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_poll_until_done_still_returns_on_a_genuine_completion(get_backend, monitor):
    """Guard against over-correcting: a COMPLETED earned after the dispatch
    boundary must still end the wait promptly."""
    get_backend.return_value = _tmux_backend()
    boundary = monitor.output_generation(TERMINAL_ID)
    monitor.notify_input_sent(TERMINAL_ID)

    def script(n):
        if n == 1:
            _latch(monitor, TerminalStatus.PROCESSING, 9)
        elif n == 2:
            _latch(monitor, TerminalStatus.COMPLETED, 10)

    reads, get = _http_reader(monitor, boundary, script)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        poll_until_done(TERMINAL_ID, timeout=30.0, polling_interval=0.01, min_generation=boundary)

    assert len(reads) <= 3


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_poll_until_done_refuses_an_ordinary_repaint_of_a_stale_completion(get_backend, monitor):
    """An ordinary repaint must not be mistaken for a cao-server restart.

    One more chunk lands and bumps ``output_generation`` without re-stamping the
    still-COMPLETED status -- ``_apply_detection_locked`` deliberately does not
    re-stamp an unchanged re-detection (``_apply_detection``). So
    ``status_generation < boundary`` is the ordinary steady state after any
    repaint, not evidence of a restart. An earlier version of this gate inferred
    "generation went backwards means the server restarted" and disengaged
    itself here, which reopens #735 in the common case: the previous turn's
    stale COMPLETED must still be refused.

    The repaint lands BEFORE the boundary is sampled, so after the send the
    counter equals the boundary: nothing has landed for this turn, and the
    refusal is unbounded in that state (the bounded cases are below).
    """
    get_backend.return_value = _tmux_backend()
    with monitor._lock:
        monitor._output_generation[TERMINAL_ID] += 1  # repaint, no re-stamp
    boundary = monitor.output_generation(TERMINAL_ID)
    assert boundary == 9
    monitor.notify_input_sent(TERMINAL_ID)

    reads, get = _http_reader(monitor, boundary, lambda n: None)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        with pytest.raises(click.ClickException, match="Timed out"):
            poll_until_done(
                TERMINAL_ID, timeout=0.2, polling_interval=0.01, min_generation=boundary
            )

    assert len(reads) > 1


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_poll_until_done_degrades_when_the_server_names_no_generation(get_backend, monitor):
    """An older cao-server (or herdr) reports no ``status_generation``; the wait
    must fall back to today's frame-based behaviour, never hang."""
    get_backend.return_value = _tmux_backend()
    monitor.notify_input_sent(TERMINAL_ID)

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": TerminalStatus.COMPLETED.value}

    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", return_value=response):
        poll_until_done(TERMINAL_ID, timeout=5.0, polling_interval=0.01, min_generation=8)


@patch("cli_agent_orchestrator.backends.registry.get_backend")
def test_an_error_is_honoured_regardless_of_generation(get_backend, monitor):
    """A dead provider is not a stale frame: ERROR must still raise even when it
    carries a pre-dispatch generation."""
    get_backend.return_value = _tmux_backend()
    monitor.notify_input_sent(TERMINAL_ID)

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": TerminalStatus.ERROR.value, "status_generation": 1}

    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", return_value=response):
        with pytest.raises(click.ClickException, match="ERROR"):
            poll_until_done(TERMINAL_ID, timeout=5.0, polling_interval=0.01, min_generation=8)


# ---------------------------------------------------------------------------
# P2-2 (round-1 review of PR #999): the refusal must be bounded.
#
# The tests above hand-set the latch. These drive the REAL latch through
# ``_process_chunk`` with a buffer-parsing provider, because the defect lives in
# two transitions ``_apply_detection_locked`` deliberately does not stamp:
# COMPLETED -> COMPLETED (an unchanged re-detection) and the COMPLETED -> IDLE ->
# COMPLETED round trip. A turn that finishes through either keeps the previous
# turn's stamp, and a gate that refuses such a reading unconditionally makes
# `cao session send` wait out its whole timeout on a turn that is done.
# ---------------------------------------------------------------------------


class _RawProvider:
    """A provider whose verdict is a marker in the tail of the rolling buffer."""

    supports_screen_detection = False

    def get_status(self, buffer):
        tail = buffer[-200:]
        if "<DONE>" in tail:
            return TerminalStatus.COMPLETED
        if "<SPIN>" in tail:
            return TerminalStatus.PROCESSING
        if "<PROMPT>" in tail:
            return TerminalStatus.IDLE
        return TerminalStatus.UNKNOWN


@pytest.fixture
def chunk_driven():
    """A real StatusMonitor whose latch only moves through ``_process_chunk``.

    With no event loop running, detection runs inline on every chunk, as it does
    in production while the status is ready.
    """
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend") as get_backend,
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_RawProvider(),
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.get_server_settings",
            return_value={"state_buffer_max": 32768},
        ),
    ):
        get_backend.return_value = _tmux_backend()
        yield StatusMonitor()


def _dispatch(sm):
    """What ``dispatch_input`` does around the paste: arm, clear, sample the boundary."""
    sm.notify_input_sent(TERMINAL_ID)
    sm.clear_rolling_buffer(TERMINAL_ID)
    return sm.output_generation(TERMINAL_ID)


def _finish_a_first_turn(sm):
    _dispatch(sm)
    sm._process_chunk(TERMINAL_ID, "task one <SPIN>")
    sm._process_chunk(TERMINAL_ID, " answer one <DONE>")
    assert sm.status_observation(TERMINAL_ID).status == TerminalStatus.COMPLETED


# Every way the latch can reach COMPLETED for a new turn WITHOUT moving the stamp
# past the dispatch boundary. If ``_apply_detection_locked`` grows another
# unstamped transition, add it here: the gate must stay bounded across the class.
UNSTAMPED_COMPLETIONS = [
    pytest.param(["task two <SPIN> answer two <DONE>"], id="completed-to-completed-in-one-chunk"),
    pytest.param(
        ["task two <PROMPT>", " <SPIN> answer two <DONE>"],
        id="completed-to-idle-to-completed-round-trip",
    ),
]


@pytest.mark.parametrize("turn_two", UNSTAMPED_COMPLETIONS)
def test_a_completion_the_latch_does_not_stamp_still_ends_the_wait_within_the_grace(
    chunk_driven, turn_two, capsys
):
    """P2-2: the gate must not refuse a finished turn until the timeout.

    Before the fix both parametrizations raised "Timed out" however long the
    timeout was; the merge-base wait returned at once.
    """
    sm = chunk_driven
    _finish_a_first_turn(sm)
    boundary = _dispatch(sm)
    for chunk in turn_two:
        sm._process_chunk(TERMINAL_ID, chunk)

    # The shape the review named: output landed for this turn, the status reads
    # COMPLETED, and the stamp is still the previous turn's.
    observation = sm.status_observation(TERMINAL_ID)
    assert observation.status == TerminalStatus.COMPLETED
    assert observation.output_generation <= boundary
    assert sm.output_generation(TERMINAL_ID) > boundary

    reads, get = _http_reader(sm, boundary, lambda n: None)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        started = time.monotonic()
        poll_until_done(
            TERMINAL_ID,
            timeout=10.0,
            polling_interval=0.01,
            min_generation=boundary,
            stamp_grace=0.2,
        )
        elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"the gate held for {elapsed:.1f}s past a finished turn"
    assert len(reads) > 1, "the stale reading was accepted at once; the gate did not engage"
    err = capsys.readouterr().err
    assert "not re-earned" in err and "previous turn" in err, err


def test_a_completion_whose_processing_was_sampled_needs_no_grace_and_no_warning(
    chunk_driven, capsys
):
    """Control: the ordinary turn is stamped, so it finishes on the stamp alone."""
    sm = chunk_driven
    _finish_a_first_turn(sm)
    boundary = _dispatch(sm)
    sm._process_chunk(TERMINAL_ID, "task two <SPIN>")
    sm._process_chunk(TERMINAL_ID, " answer two <DONE>")
    assert sm.status_observation(TERMINAL_ID).output_generation > boundary

    reads, get = _http_reader(sm, boundary, lambda n: None)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        poll_until_done(
            TERMINAL_ID,
            timeout=10.0,
            polling_interval=0.01,
            min_generation=boundary,
            stamp_grace=60.0,  # never reached: the stamp itself opens the gate
        )

    assert len(reads) == 1
    assert capsys.readouterr().err == ""


def test_within_the_grace_the_previous_turns_completion_is_still_refused(chunk_driven):
    """The bound must not be a bypass: #735's own shape -- the paste echoed, the
    worker has not started, the screen still shows the previous answer -- is
    refused for the whole grace."""
    sm = chunk_driven
    _finish_a_first_turn(sm)
    boundary = _dispatch(sm)
    sm._process_chunk(TERMINAL_ID, "task two")  # the echo: output landed, no verdict
    observation = sm.status_observation(TERMINAL_ID)
    assert observation.status == TerminalStatus.COMPLETED
    assert observation.output_generation <= boundary < sm.output_generation(TERMINAL_ID)

    reads, get = _http_reader(sm, boundary, lambda n: None)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        with pytest.raises(click.ClickException, match="Timed out"):
            poll_until_done(
                TERMINAL_ID,
                timeout=0.2,
                polling_interval=0.01,
                min_generation=boundary,
                stamp_grace=30.0,
            )
    assert len(reads) > 1


def test_with_no_output_since_the_send_the_refusal_has_no_bound(chunk_driven, capsys):
    """Nothing has landed: there is no turn to be done yet, so the grace never
    starts, however short it is. Ending here on the previous frame IS #735."""
    sm = chunk_driven
    _finish_a_first_turn(sm)
    boundary = _dispatch(sm)
    assert sm.output_generation(TERMINAL_ID) == boundary

    reads, get = _http_reader(sm, boundary, lambda n: None)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        with pytest.raises(click.ClickException, match="Timed out") as exc:
            poll_until_done(
                TERMINAL_ID,
                timeout=0.3,
                polling_interval=0.01,
                min_generation=boundary,
                stamp_grace=0.01,
            )
    assert len(reads) > 1
    assert capsys.readouterr().err == ""
    # The timeout says WHY the gate held (re-review note): the operator should
    # not have to infer "the status never moved" from "time ran out".
    assert "no output has landed since the send" in exc.value.message


def test_a_transient_poll_failure_during_the_gated_wait_neither_aborts_nor_opens_the_gate(
    chunk_driven, capsys
):
    """#1037's retry and the #735 gate together: a 503 while the previous turn's
    COMPLETED is still latched is retried (not fatal) and is not a reading, so it
    neither counts as a stamp nor starts the grace; the wait then ends on this
    turn's own stamped completion with no warning."""
    sm = chunk_driven
    _finish_a_first_turn(sm)
    boundary = _dispatch(sm)

    def script(n):
        if n in (1, 3):
            raise api_http.exceptions.HTTPError(
                "503 Service Unavailable", response=MagicMock(status_code=503)
            )
        if n == 5:
            sm._process_chunk(TERMINAL_ID, "task two <SPIN>")
        elif n == 7:
            sm._process_chunk(TERMINAL_ID, " answer two <DONE>")

    reads, get = _http_reader(sm, boundary, script)
    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", side_effect=get):
        poll_until_done(
            TERMINAL_ID,
            timeout=10.0,
            polling_interval=0.01,
            min_generation=boundary,
            stamp_grace=0.01,  # would open the gate at once if a 503 started it
        )

    assert len(reads) >= 7, "a failed poll or the stale COMPLETED ended the wait early"
    assert capsys.readouterr().err == "", "a transient failure must not degrade the gate"


def test_a_server_restart_degrades_at_once_with_a_warning(capsys):
    """After a cao-server restart every generation starts again from 0, so the
    counter sits BELOW the boundary this send was given -- impossible within one
    server lifetime. Refusing on would wait out the whole timeout (review note);
    the wait must say what happened and fall back to the frame-based rules."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": TerminalStatus.COMPLETED.value,
        "status_generation": 0,
        "output_generation": 0,
    }

    with patch("cli_agent_orchestrator.utils.terminal.api_http.get", return_value=response):
        started = time.monotonic()
        poll_until_done(TERMINAL_ID, timeout=10.0, polling_interval=0.01, min_generation=8)
        elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert "restarted" in capsys.readouterr().err


def test_send_input_wrapper_still_keeps_its_bool_contract():
    """``send_input`` collapses the boundary to ``True`` by design; the HTTP
    endpoint calls ``dispatch_input`` directly instead so the boundary reaches the
    wire (api/main.py send_terminal_input)."""
    import cli_agent_orchestrator.services.terminal_service as ts

    with patch.object(ts, "dispatch_input", return_value=42) as dispatch:
        assert ts.send_input(TERMINAL_ID, "hello") is True
    assert dispatch.called
