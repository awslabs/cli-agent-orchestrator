"""Fixture-backed tests for the OMP provider."""

import asyncio
import json
import re
import shlex
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.omp import OmpProvider, ProviderError
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_profile(**kwargs) -> AgentProfile:
    return AgentProfile(name="analyst", description="test profile", **kwargs)


def make_provider(**kwargs) -> OmpProvider:
    return OmpProvider(
        terminal_id="terminal-123",
        session_name="test-session",
        window_name="window-0",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def native_status_is_unavailable():
    """Exercise the fixture detector, not a backend-native shortcut."""
    with patch.object(OmpProvider, "_resolve_native_status", return_value=None):
        yield


# ── Capabilities and lifecycle state ────────────────────────────────────


def test_provider_capabilities_and_exit_command():
    provider = make_provider()

    assert provider.paste_enter_count == 1
    assert provider.paste_submit_delay == 0.3
    assert provider.supports_screen_detection is True
    assert provider.supports_direct_status_probe is False
    assert provider.blocks_orchestrated_input_while_waiting_user_answer is True
    assert provider.accepts_input_while_processing is False
    assert provider.exit_cli() == "/exit"


def test_omp_restrictions_are_explicitly_soft():
    from cli_agent_orchestrator.models.provider import ProviderType
    from cli_agent_orchestrator.services.terminal_service import (
        RUNTIME_SKILL_PROMPT_PROVIDERS,
        SOFT_ENFORCEMENT_PROVIDERS,
    )
    from cli_agent_orchestrator.utils.tool_mapping import TOOL_MAPPING

    assert ProviderType.OMP.value in RUNTIME_SKILL_PROMPT_PROVIDERS
    assert ProviderType.OMP.value in SOFT_ENFORCEMENT_PROVIDERS
    assert ProviderType.OMP.value not in TOOL_MAPPING


def test_status_fixtures_and_turn_split():
    provider = make_provider()

    assert provider.get_status(load_fixture("omp_idle.txt")) == TerminalStatus.IDLE
    assert provider.get_status(load_fixture("omp_processing.txt")) == TerminalStatus.PROCESSING
    assert (
        provider.get_status(load_fixture("omp_waiting.txt")) == TerminalStatus.WAITING_USER_ANSWER
    )
    assert provider.get_status(load_fixture("omp_error.txt")) == TerminalStatus.ERROR
    assert provider.get_status(load_fixture("omp_prose.txt")) == TerminalStatus.IDLE
    assert provider.get_status("Error: failed to parse input\n╰─ ready ─╯\n") == TerminalStatus.IDLE

    provider.mark_input_received()
    assert provider.get_status(load_fixture("omp_completed.txt")) == TerminalStatus.COMPLETED


def test_status_unknown_and_native_fallback():
    provider = make_provider()
    assert provider.get_status("") == TerminalStatus.UNKNOWN
    assert provider.get_status(None) == TerminalStatus.UNKNOWN

    with patch.object(provider, "_resolve_native_status", return_value=TerminalStatus.PROCESSING):
        assert provider.get_status(load_fixture("omp_idle.txt")) == TerminalStatus.PROCESSING


def test_raw_cursor_redraw_fixture_is_processing():
    assert (
        make_provider().get_status(load_fixture("omp_processing.raw.txt"))
        == TerminalStatus.PROCESSING
    )


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("Allow tool: bash", TerminalStatus.IDLE),
        ("Error: No model selected.", TerminalStatus.IDLE),
        ("Working… ⟨esc⟩", TerminalStatus.IDLE),
    ],
)
def test_later_ready_status_line_makes_active_marker_stale(marker, expected):
    provider = make_provider()
    buffer = f"{marker}\nin: 1 out: 2 t: 1s tok/s: 1"

    assert provider.get_status(buffer) == expected


@pytest.mark.parametrize(
    "marker",
    [
        "Allow tool: bash",
        "Working… ⟨esc⟩",
        "Error: No model selected.",
    ],
)
def test_later_ready_frame_makes_active_marker_stale_without_status_line(marker):
    provider = make_provider()
    frame = f"{marker}\n╰─ ready ─╯"

    assert provider.get_status(frame) == TerminalStatus.IDLE
    provider.mark_input_received()
    assert provider.get_status(frame) == TerminalStatus.COMPLETED
    assert provider.get_status_from_screen([marker, "╰─ ready ─╯"]) == (TerminalStatus.COMPLETED)


@pytest.mark.parametrize(
    "fixture",
    ["omp_processing.raw.txt", "omp_waiting.txt", "omp_error.txt"],
)
def test_completed_transcript_retires_stale_raw_markers(fixture):
    provider = make_provider()
    provider.mark_input_received()
    output = load_fixture(fixture) + "\n" + load_fixture("omp_completed.txt")

    assert provider.get_status(output) == TerminalStatus.COMPLETED


def test_live_model_error_companion_survives_its_ready_footer():
    assert make_provider().get_status(load_fixture("omp_error.txt")) == TerminalStatus.ERROR


@pytest.mark.parametrize(
    "output",
    [
        "Allow tool: bash\n❯ Approve\nin: 1 out: 2 t: 1s tok/s: 1",
        "Error: No model selected.\nUse /login, set an API key\nin: 1 out: 2 t: 1s tok/s: 1",
        "⠋ Working… ⟨esc⟩\nin: 1 out: 2 t: 1s tok/s: 1",
    ],
)
def test_later_status_line_is_stale_even_with_live_companion(output):
    assert make_provider().get_status(output) == TerminalStatus.IDLE


def test_live_spinner_wins_over_complete_stale_approval_block():
    output = load_fixture("omp_waiting.txt") + "\n╰─ ready ─╯\n⠋ Working… ⟨esc⟩"

    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_live_spinner_wins_over_complete_stale_model_error_block():
    output = load_fixture("omp_error.txt") + "\n╰─ ready ─╯\n⠋ Working… ⟨esc⟩"

    assert make_provider().get_status(output) == TerminalStatus.PROCESSING


def test_later_live_approval_wins_over_stale_error_block():
    output = load_fixture("omp_error.txt") + "\n╰─ ready ─╯\n" + load_fixture("omp_waiting.txt")

    assert make_provider().get_status(output) == TerminalStatus.WAITING_USER_ANSWER


def test_screen_detection_uses_same_precedence():
    provider = make_provider()
    assert (
        provider.get_status_from_screen(["Allow tool: bash", "in: 1 out: 2 t: 1s tok/s: 1"])
        == TerminalStatus.IDLE
    )
    assert provider.get_status_from_screen(["Working… ⟨esc⟩"]) == TerminalStatus.PROCESSING

    provider.mark_input_received()
    assert (
        provider.get_status_from_screen(["in: 1 out: 2 cache 1 t: 1s tok/s: 1"])
        == TerminalStatus.COMPLETED
    )


# ── OMP 18.x TUI (keycap-led working row, box and band composers) ──────


OMP18_LIVE_FIXTURES = [
    "omp18_box_processing.txt",
    "omp18_band_processing.txt",
    "omp18_band_tool_running.txt",
]
OMP18_READY_FIXTURES = [
    "omp18_band_idle.txt",
    "omp18_box_completed.txt",
    "omp18_band_completed.txt",
]


def _screen_lines(fixture: str) -> list[str]:
    return strip_terminal_escapes(load_fixture(fixture)).splitlines()


@pytest.mark.parametrize("fixture", OMP18_LIVE_FIXTURES)
def test_omp18_working_row_is_processing(fixture):
    provider = make_provider()

    assert provider.get_status(load_fixture(fixture)) == TerminalStatus.PROCESSING
    assert provider.get_status_from_screen(_screen_lines(fixture)) == TerminalStatus.PROCESSING


@pytest.mark.parametrize("fixture", OMP18_READY_FIXTURES)
def test_omp18_ready_frames_are_idle_then_completed(fixture):
    provider = make_provider()
    assert provider.get_status(load_fixture(fixture)) == TerminalStatus.IDLE

    provider.mark_input_received()
    assert provider.get_status(load_fixture(fixture)) == TerminalStatus.COMPLETED
    assert provider.get_status_from_screen(_screen_lines(fixture)) == TerminalStatus.COMPLETED


@pytest.mark.parametrize(
    ("live", "done"),
    [
        ("omp18_box_processing.txt", "omp18_box_completed.txt"),
        ("omp18_band_processing.txt", "omp18_band_completed.txt"),
        ("omp18_band_tool_running.txt", "omp18_band_completed.txt"),
    ],
)
def test_omp18_completed_turn_retires_stale_working_row(live, done):
    provider = make_provider()
    provider.mark_input_received()

    assert provider.get_status(load_fixture(live) + load_fixture(done)) == (
        TerminalStatus.COMPLETED
    )


@pytest.mark.parametrize(
    "marker", ["⎋ Working…", "⎋ Run requested delay", "Working… ⟨esc⟩", "⠋ Running bash ⟨esc⟩"]
)
def test_both_working_row_grammars_are_live_above_either_frame(marker):
    for tail in (
        "╭── OMP session ──╮\n╰─ ready ─╯",
        "╭── π > Model ──╮\n╰─ ─╯",
        " π > Model ─\n╰─",
    ):
        assert make_provider().get_status(f"{marker}\n{tail}") == TerminalStatus.PROCESSING


def test_omp18_keycap_in_prose_after_ready_frame_is_stale():
    output = load_fixture("omp18_band_processing.txt") + "\n ACK\n π > Model ─\n╰─"

    provider = make_provider()
    provider.mark_input_received()
    assert provider.get_status(output) == TerminalStatus.COMPLETED


@pytest.mark.parametrize("fixture", ["omp18_box_completed.txt", "omp18_band_completed.txt"])
def test_omp18_extracts_final_response_under_theme_background(fixture):
    assert make_provider().extract_last_message_from_script(load_fixture(fixture)) == "ACK"


# ── Dispatch attribution: a ready frame after a dispatch is not yet this turn ──


ACK_PROMPT = "Reply with exactly ACK and do not use any tools."


def _dispatch(provider: OmpProvider, message: str = ACK_PROMPT) -> None:
    provider.mark_input_received()
    provider.record_dispatched_message(message)


def _echo_only(fixture: str) -> list[str]:
    """The completed screen with the response row removed: prompt echoed, turn not started."""
    return [line for line in _screen_lines(fixture) if line.strip() != "ACK"]


@pytest.mark.parametrize("fixture", ["omp18_band_idle.txt", "omp18_box_completed.txt"])
def test_previous_ready_screen_after_dispatch_is_not_completion(fixture):
    provider = make_provider()
    provider.get_status_from_screen(_screen_lines(fixture))
    _dispatch(provider, "Summarise the repository layout.")

    # The tail of OMP's startup paint lands after the dispatch boundary.
    provider.record_status_chunk(load_fixture(fixture), epoch=1)
    assert provider.get_status(load_fixture(fixture)) == TerminalStatus.PROCESSING
    assert provider.get_status_from_screen(_screen_lines(fixture)) == TerminalStatus.PROCESSING


@pytest.mark.parametrize("fixture", ["omp18_box_completed.txt", "omp18_band_completed.txt"])
def test_echoed_prompt_before_the_spinner_is_still_pending(fixture):
    provider = make_provider()
    _dispatch(provider)

    assert provider.get_status_from_screen(_echo_only(fixture)) == TerminalStatus.PROCESSING


def test_first_turn_frame_keycap_over_idle_status_row_is_processing():
    # Observed live: the turn's first frame draws ``⎋ Working…`` above a ``π`` row.
    provider = make_provider()
    _dispatch(provider)
    frame = ["  ⎋ Working…", "╭── π > Model > /tmp/cao-omp-fixtures ──╮", "╰─  ─╯"]

    assert provider.get_status_from_screen(frame) == TerminalStatus.PROCESSING
    assert provider.get_status("\n".join(frame)) == TerminalStatus.PROCESSING


@pytest.mark.parametrize(
    "live", ["omp18_band_processing.txt", "omp18_box_processing.txt", "omp18_band_tool_running.txt"]
)
def test_turn_seen_working_then_ready_is_completed(live):
    provider = make_provider()
    _dispatch(provider)
    done = live.replace("tool_running", "completed").replace("processing", "completed")

    assert provider.get_status_from_screen(_screen_lines(live)) == TerminalStatus.PROCESSING
    assert provider.get_status_from_screen(_screen_lines(done)) == TerminalStatus.COMPLETED


def test_fast_turn_seen_only_in_chunks_is_completed():
    provider = make_provider()
    _dispatch(provider)
    provider.record_status_chunk(load_fixture("omp18_band_processing.txt"), epoch=1)

    assert (
        provider.get_status_from_screen(_screen_lines("omp18_band_completed.txt"))
        == TerminalStatus.COMPLETED
    )


def test_long_turn_whose_frames_left_the_buffer_completes_from_chunk_activity():
    provider = make_provider()
    _dispatch(provider, "A prompt that is no longer in the cropped buffer.")
    provider.record_status_chunk(load_fixture("omp18_band_processing.txt"), epoch=1)

    cropped = [f"line {n} of a long answer" for n in range(40)] + [" π > Model ─", "╰─"]
    assert provider.get_status_from_screen(cropped) == TerminalStatus.COMPLETED


def test_chunks_before_the_dispatch_are_not_turn_activity():
    provider = make_provider()
    provider.record_status_chunk(load_fixture("omp18_band_processing.txt"), epoch=0)
    _dispatch(provider, "Summarise the repository layout.")

    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.PROCESSING


def test_redelivery_waits_for_its_own_turn():
    provider = make_provider()
    _dispatch(provider, "Summarise the repository layout.")
    provider.mark_redelivery_received()
    provider.record_dispatched_message("Summarise the repository layout.")

    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.PROCESSING
    provider.record_status_chunk(load_fixture("omp18_band_processing.txt"), epoch=2)
    assert provider.get_status(load_fixture("omp18_band_completed.txt")) == (
        TerminalStatus.COMPLETED
    )


def test_spinner_status_row_stays_live_while_the_working_row_is_not_redrawn():
    # 18.x repaints only the status row per spinner frame; the working row is drawn once.
    raw = (
        "  ⎋ Working…\n╭── ⠋ 0s > Model ──╮\n╰─  ─╯\n"
        "╭── ⠙ 1s > Model ──╮\n╭── ⠹ 2s > Model ──╮\n\n╰─  ─╯"
    )
    assert make_provider().get_status(raw) == TerminalStatus.PROCESSING


def test_idle_status_row_after_spinner_rows_retires_the_turn():
    raw = (
        "  ⎋ Working…\n╭── ⠋ 0s > Model ──╮\n╰─  ─╯\n ACK\n" "╭── π > Model ──╮\n╭── π > Model ──╮"
    )
    provider = make_provider()
    _dispatch(provider)
    provider.record_status_chunk(raw, epoch=1)
    assert provider.get_status(raw) == TerminalStatus.COMPLETED


# ── OSC 7501 Program Status reports (OMP 18) ────────────────────────────


TOOL_TURN_PROMPT = "Use the bash tool to run exactly: sleep 3 && echo DONE. Then reply DONE."


def _osc7501(state: str) -> str:
    return f"\x1b]7501;state={state}:app=omp\x1b\\"


def _replay(
    provider: OmpProvider, stream: str, size: int, edges: tuple[int, ...] = ()
) -> list[TerminalStatus]:
    """Feed ``stream`` as the monitor does: per chunk, then classify the cropped buffer.

    Chunks are ``size`` bytes, also cut at each offset in ``edges``.
    """
    statuses, buffer = [], ""
    cuts = sorted({*range(0, len(stream), size), *edges, len(stream)} - {0})
    for start, end in zip([0, *cuts], cuts):
        chunk = stream[start:end]
        provider.record_status_chunk(chunk, epoch=1)
        buffer = (buffer + chunk)[-32768:]
        status = provider.get_status(buffer)
        if not statuses or statuses[-1] != status:
            statuses.append(status)
    return statuses


@pytest.mark.parametrize("size", [37, 333, 1024, 4096])
def test_recorded_tool_turn_reads_processing_then_completed(size):
    raw = load_fixture("omp18_tool_turn.raw.txt")
    dispatch = raw.index(TOOL_TURN_PROMPT) - 150
    provider = make_provider()
    provider.record_status_chunk(raw[:dispatch], epoch=0)
    assert provider.get_status(raw[:dispatch]) == TerminalStatus.IDLE

    _dispatch(provider, TOOL_TURN_PROMPT)
    assert _replay(provider, raw[dispatch:], size) == [
        TerminalStatus.PROCESSING,
        TerminalStatus.COMPLETED,
    ]


@pytest.mark.parametrize("size", [37, 333, 1024, 4096])
def test_recorded_tool_turn_without_reports_reads_processing_then_completed(size):
    """OMP before 18.8.7, or with ``terminal.programStatus`` off, sends no OSC 7501.

    The screen markers alone must then hold PROCESSING through the turn: OMP 18
    also draws ``╰─`` frames mid-turn (the band frame, tool-box bottoms), which
    must not retire the spinner status row.
    """
    raw = re.sub(
        r"\x1b\]7501;[^\x07\x1b]*(?:\x07|\x1b\\)", "", load_fixture("omp18_tool_turn.raw.txt")
    )
    dispatch = raw.index(TOOL_TURN_PROMPT) - 150
    provider = make_provider()
    provider.record_status_chunk(raw[:dispatch], epoch=0)
    assert provider._program_status is None

    _dispatch(provider, TOOL_TURN_PROMPT)
    # StatusMonitor never lets UNKNOWN (no marker drawn yet) replace a known status.
    statuses = [s for s in _replay(provider, raw[dispatch:], size) if s != TerminalStatus.UNKNOWN]
    assert statuses == [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]


def test_idle_report_as_the_paste_lands_is_not_completion():
    provider = make_provider()
    provider.record_status_chunk(_osc7501("done"), epoch=0)
    _dispatch(provider)
    idle_screen = load_fixture("omp18_band_idle.txt")

    provider.record_status_chunk(_osc7501("idle"), epoch=1)
    assert provider.get_status(idle_screen) == TerminalStatus.PROCESSING
    provider.record_status_chunk(_osc7501("working"), epoch=1)
    assert provider.get_status(idle_screen) == TerminalStatus.PROCESSING
    provider.record_status_chunk(_osc7501("done"), epoch=1)
    assert provider.get_status(idle_screen) == TerminalStatus.COMPLETED


def test_report_split_across_chunks_is_read_once_complete():
    provider = make_provider()
    _dispatch(provider)
    report = _osc7501("working")

    provider.record_status_chunk(report[:-1], epoch=1)
    assert provider._program_status is None
    provider.record_status_chunk(report[-1:], epoch=1)
    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.PROCESSING


def test_report_split_right_after_its_esc_is_read():
    """A FIFO read can end on the ESC that opens a report; OMP never repeats it."""
    provider = make_provider()
    _dispatch(provider)
    report = _osc7501("working")

    provider.record_status_chunk("output" + report[:1], epoch=1)
    provider.record_status_chunk(report[1:], epoch=1)
    assert provider._program_status == "working"
    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.PROCESSING


def _recorded_turn_report_edges() -> list[int]:
    raw = load_fixture("omp18_tool_turn.raw.txt")
    dispatch = raw.index(TOOL_TURN_PROMPT) - 150
    stream = raw[dispatch:]
    return [i for i in range(len(stream)) if stream.startswith("\x1b]7501;", i)]


_REPORT_EDGE_CASES = [(i,) for i in range(len(_recorded_turn_report_edges()))] + [
    tuple(range(len(_recorded_turn_report_edges())))
]


@pytest.mark.parametrize("size", [37, 4096])
@pytest.mark.parametrize("reports", _REPORT_EDGE_CASES)
def test_recorded_tool_turn_survives_an_edge_after_each_report_esc(reports, size):
    raw = load_fixture("omp18_tool_turn.raw.txt")
    dispatch = raw.index(TOOL_TURN_PROMPT) - 150
    stream = raw[dispatch:]
    offsets = _recorded_turn_report_edges()
    edges = tuple(offsets[i] + 1 for i in reports)
    provider = make_provider()
    provider.record_status_chunk(raw[:dispatch], epoch=0)
    _dispatch(provider, TOOL_TURN_PROMPT)

    assert _replay(provider, stream, size, edges=edges) == [
        TerminalStatus.PROCESSING,
        TerminalStatus.COMPLETED,
    ]


def test_working_report_beats_a_ready_looking_screen():
    provider = make_provider()
    provider.record_status_chunk(_osc7501("working"), epoch=0)

    assert provider.get_status(load_fixture("omp18_band_completed.txt")) == (
        TerminalStatus.PROCESSING
    )


def test_blocked_report_is_waiting_and_drawn_approval_wins_over_working():
    provider = make_provider()
    provider.record_status_chunk(_osc7501("blocked:kind=permission"), epoch=0)
    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == (
        TerminalStatus.WAITING_USER_ANSWER
    )

    provider.record_status_chunk(_osc7501("working"), epoch=0)
    assert provider.get_status(load_fixture("omp_waiting.txt")) == (
        TerminalStatus.WAITING_USER_ANSWER
    )


def test_error_report_after_the_turn_ran_is_error_but_a_stale_one_is_not():
    provider = make_provider()
    provider.record_status_chunk(_osc7501("error:msg=QVBJIGVycm9y"), epoch=0)
    _dispatch(provider)
    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.PROCESSING

    provider.record_status_chunk(_osc7501("working") + _osc7501("error:msg=QVBJIGVycm9y"), epoch=1)
    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.ERROR


def test_clear_report_falls_back_to_the_screen():
    provider = make_provider()
    provider.record_status_chunk(_osc7501("working") + _osc7501("clear"), epoch=0)

    assert provider.get_status(load_fixture("omp18_band_idle.txt")) == TerminalStatus.IDLE


@pytest.mark.parametrize("screen", ["omp18_band_idle.txt", "omp18_band_processing.txt"])
def test_unknown_report_state_falls_back_to_the_screen(screen):
    """A state OMP may add later carries no verdict; the screen decides."""
    provider = make_provider()
    provider.record_status_chunk(_osc7501("working") + _osc7501("paused"), epoch=0)
    assert provider._program_status == "paused"

    reference = make_provider()
    assert provider.get_status(load_fixture(screen)) == reference.get_status(load_fixture(screen))


def test_raw_status_reports_error_when_omp_exits():
    provider = make_provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "zsh"

        assert (
            provider.get_status("Allow tool: bash\n╰─ stale ready frame ─╯") == TerminalStatus.ERROR
        )

    backend.return_value.get_pane_current_command.assert_called_once_with(
        "test-session", "window-0"
    )


def test_rendered_screen_status_reports_error_when_omp_exits():
    provider = make_provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "zsh"

        assert provider.get_status_from_screen(["Allow tool: bash", "╰─ ready ─╯"]) == (
            TerminalStatus.ERROR
        )

    backend.return_value.get_pane_current_command.assert_called_once_with(
        "test-session", "window-0"
    )


def test_live_omp_command_does_not_trigger_shell_exit_error():
    provider = make_provider()
    provider._initialized = True
    provider.shell_baseline = "zsh"
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        backend.return_value.get_pane_current_command.return_value = "omp"

        assert provider.get_status("Working… ⟨esc⟩") == TerminalStatus.PROCESSING

    backend.return_value.get_pane_current_command.assert_called_once_with(
        "test-session", "window-0"
    )


@pytest.mark.parametrize(
    ("initialized", "baseline"),
    [(False, "zsh"), (True, None)],
)
def test_shell_exit_probe_skips_uninitialized_or_missing_baseline(initialized, baseline):
    provider = make_provider()
    provider._initialized = initialized
    provider.shell_baseline = baseline
    with patch("cli_agent_orchestrator.providers.omp.get_backend") as backend:
        assert provider.get_status("╰─ ready ─╯") == TerminalStatus.IDLE

    backend.return_value.get_pane_current_command.assert_not_called()


# ── Launch command and generated artifacts ──────────────────────────────


def _command_parts(provider: OmpProvider) -> list[str]:
    with patch(
        "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
    ):
        return shlex.split(provider._build_omp_command())


def test_command_omits_empty_context_and_preserves_native_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)

    parts = _command_parts(make_provider())

    overlay = tmp_path / "tmp" / "omp" / "terminal-123" / "cao-input.yml"
    assert parts == ["omp", "--config", str(overlay)]
    assert sorted(path.name for path in overlay.parent.iterdir()) == ["cao-input.yml"]


def test_command_overlay_only_disables_large_paste_menu(tmp_path, monkeypatch):
    """OMP 18.3.1+ opens a menu for pastes of 100+ lines; CAO's submitting Enter
    would pick its first entry and leave the task staged in the composer."""
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)

    parts = _command_parts(make_provider())

    overlay = Path(parts[parts.index("--config") + 1])
    assert overlay.read_text(encoding="utf-8") == "paste:\n  largeMenuThreshold: 0\n"
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600
    assert parts.count("--config") == 1


def test_command_prefers_explicit_model_and_writes_private_append_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    profile = make_profile(system_prompt="Profile role", model="profile-model")
    provider = make_provider(
        agent_profile="analyst",
        allowed_tools=["fs_read", "@cao-mcp-server"],
        skill_prompt="## Available Skills\n- test-skill",
        model="explicit model",
    )

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
        patch(
            "cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile
        ) as load,
    ):
        parts = shlex.split(provider._build_omp_command())

    load.assert_called_once_with("analyst")
    assert parts[:3] == ["omp", "--model", "explicit model"]
    assert "--append-system-prompt" in parts
    assert "--system-prompt" not in parts
    assert not {
        "--profile",
        "--no-tools",
        "--tools",
        "--no-skills",
        "--no-rules",
        "--no-extensions",
        "--auto-approve",
        "--approval-mode",
    }.intersection(parts)

    context_path = Path(parts[parts.index("--append-system-prompt") + 1])
    assert context_path.read_text(encoding="utf-8") == (
        SECURITY_PROMPT
        + "\nYou only have access to these tools: fs_read, @cao-mcp-server\n"
        + "Profile role\n\n## Available Skills\n- test-skill"
    )
    assert stat.S_IMODE(context_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(context_path.parent.stat().st_mode) == 0o700


def test_command_uses_profile_model_when_no_explicit_model(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    profile = make_profile(model="profile-model")
    provider = make_provider(agent_profile="analyst")

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
        patch("cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile),
    ):
        parts = shlex.split(provider._build_omp_command())

    assert parts[:3] == ["omp", "--model", "profile-model"]
    assert parts[3] == "--config" and parts[4].endswith("cao-input.yml")
    assert len(parts) == 5


def test_extension_root_merges_mcp_without_overriding_explicit_terminal_id(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    profile = make_profile(
        mcpServers={
            "cao": {"command": "cao-mcp-server", "args": ["--stdio"], "env": {"KEEP": "yes"}},
            "custom": {"command": "custom-mcp", "env": {"CAO_TERMINAL_ID": "explicit"}},
            "remote": {"type": "http", "url": "https://mcp.example.test"},
        }
    )
    provider = make_provider(agent_profile="analyst")

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
        patch("cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile),
        patch(
            "cli_agent_orchestrator.providers.omp.resolve_mcp_server_config",
            side_effect=lambda config, persisted, name=None: config,
        ) as resolve,
    ):
        parts = shlex.split(provider._build_omp_command())

    extension_dir = Path(parts[parts.index("--extension") + 1])
    assert (extension_dir / "index.js").read_text(
        encoding="utf-8"
    ) == "export default function () {}"
    config = json.loads((extension_dir / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert config["cao"]["env"] == {"KEEP": "yes", "CAO_TERMINAL_ID": "terminal-123"}
    assert config["custom"]["env"] == {"CAO_TERMINAL_ID": "explicit"}
    assert config["remote"] == {"type": "http", "url": "https://mcp.example.test"}
    assert resolve.call_count == 3
    assert all(call.kwargs["persisted"] is False for call in resolve.call_args_list)
    assert stat.S_IMODE((extension_dir / "index.js").stat().st_mode) == 0o600
    assert stat.S_IMODE((extension_dir / ".mcp.json").stat().st_mode) == 0o600


def test_missing_binary_is_actionable():
    with patch("cli_agent_orchestrator.providers.omp.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="'omp' is not on \\$PATH"):
            make_provider()._build_omp_command()


def test_cleanup_removes_only_generated_terminal_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    provider = make_provider(skill_prompt="skill prompt")
    _command_parts(provider)
    root = tmp_path / "tmp" / "omp" / "terminal-123"
    sibling = tmp_path / "tmp" / "omp" / "other-terminal"
    sibling.mkdir(parents=True)

    provider.cleanup()

    assert not root.exists()
    assert sibling.exists()
    assert provider._initialized is False
    assert provider._turns == 0


# ── Initialization and extraction ───────────────────────────────────────


def test_initialize_starts_omp_and_waits_for_ready(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    launch = shlex.join(
        ["omp", "--config", str(tmp_path / "tmp" / "omp" / "terminal-123" / "cao-input.yml")]
    )
    backend = MagicMock()
    backend.get_pane_current_command.return_value = "zsh"
    provider = make_provider()
    with (
        patch(
            "cli_agent_orchestrator.providers.omp.wait_for_shell", new=AsyncMock(return_value=True)
        ),
        patch(
            "cli_agent_orchestrator.providers.omp.wait_until_status",
            new=AsyncMock(return_value=True),
        ) as wait,
        patch("cli_agent_orchestrator.providers.omp.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
    ):
        assert asyncio.run(provider.initialize()) is True

    assert provider.shell_baseline == "zsh"
    assert backend.method_calls == [
        call.get_pane_current_command("test-session", "window-0"),
        call.send_keys("test-session", "window-0", launch),
    ]
    backend.send_keys.assert_called_once_with("test-session", "window-0", launch)
    assert wait.call_args.args[1] == {TerminalStatus.IDLE, TerminalStatus.COMPLETED}


def test_initialize_raises_for_shell_and_ready_timeouts():
    with patch(
        "cli_agent_orchestrator.providers.omp.wait_for_shell", new=AsyncMock(return_value=False)
    ):
        with pytest.raises(TimeoutError, match="Shell initialization"):
            asyncio.run(make_provider().initialize())

    with (
        patch(
            "cli_agent_orchestrator.providers.omp.wait_for_shell", new=AsyncMock(return_value=True)
        ),
        patch(
            "cli_agent_orchestrator.providers.omp.wait_until_status",
            new=AsyncMock(return_value=False),
        ),
        patch("cli_agent_orchestrator.providers.omp.get_backend"),
        patch(
            "cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/local/bin/omp"
        ),
    ):
        with pytest.raises(TimeoutError, match="OMP initialization"):
            asyncio.run(make_provider().initialize())


def test_extracts_final_response_and_preserves_error_prose():
    provider = make_provider()
    two_turns = (
        "\x1b[48;2;5;5;5m\n first request\n\n\x1b[49m first answer\n\n"
        "\x1b[48;2;5;5;5m\n second request\n\n\x1b[49m second answer\n\n╰─ ready ─╯\n"
    )

    assert (
        provider.extract_last_message_from_script(load_fixture("omp_completed.txt"))
        == "RAW_FIXTURE"
    )
    assert provider.extract_last_message_from_script(load_fixture("omp_prose.txt")) == (
        "Error: working and cancel are ordinary prose."
    )
    assert provider.extract_last_message_from_script(two_turns) == "second answer"


def test_extract_accepts_box_prefixed_assistant_content():
    output = (
        "\x1b[48;2;5;5;5m\n user prompt\n\n"
        "\x1b[49m╭ assistant response starts with a box glyph\n"
        "and continues normally\n\n╰─ ready ─╯\n"
    )

    assert make_provider().extract_last_message_from_script(output) == (
        "╭ assistant response starts with a box glyph\nand continues normally"
    )


def test_extract_uses_ansi_turn_boundary_and_rejects_canceled_user_turn():
    completed = (
        "\x1b[48;2;5;5;5m\n user prompt\n\n"
        "\x1b[49m def greet(name):\n    return f'Hello, {name}'\n\n"
        "The function returns a greeting.\n\n╰─ ready ─╯\n"
    )
    canceled = "\x1b[48;2;5;5;5m\n user prompt\n\n╰─ ready ─╯\n"

    assert make_provider().extract_last_message_from_script(completed) == (
        "def greet(name):\n    return f'Hello, {name}'\n\nThe function returns a greeting."
    )
    with pytest.raises(ValueError, match="assistant block"):
        make_provider().extract_last_message_from_script(canceled)
    with pytest.raises(ValueError, match="user-turn boundary"):
        make_provider().extract_last_message_from_script("user prompt\n\n╰─ ready ─╯\n")
    with pytest.raises(ValueError, match="only chrome"):
        make_provider().extract_last_message_from_script(
            "\x1b[48;2;5;5;5m\n user prompt\n\n" "\x1b[49m ⠋ Working…\n\n╰─ ready ─╯\n"
        )
    with pytest.raises(ValueError, match="response frame"):
        make_provider().extract_last_message_from_script(
            "\x1b[48;2;5;5;5m\n user prompt\n\n\x1b[49m assistant response"
        )


@pytest.mark.parametrize("output", ["no footer", "Allow tool: bash\n╰─ frame ─╯\n"])
def test_extract_rejects_missing_or_chrome_only_response(output):
    with pytest.raises(ValueError):
        make_provider().extract_last_message_from_script(output)


def test_profile_load_failure_is_provider_specific():
    provider = make_provider(agent_profile="missing")
    with patch(
        "cli_agent_orchestrator.providers.omp.load_agent_profile",
        side_effect=RuntimeError("broken profile"),
    ):
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._load_profile()


def test_extension_serializes_model_config_and_status_unknown_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    mcp_model = MagicMock()
    mcp_model.model_dump.return_value = {"command": "model-mcp"}
    provider = make_provider()

    with patch(
        "cli_agent_orchestrator.providers.omp.resolve_mcp_server_config",
        side_effect=lambda config, persisted, name=None: config,
    ):
        provider._write_extension_root({"model": mcp_model})

    assert mcp_model.model_dump.called
    assert provider.get_status("unrecognized OMP text") == TerminalStatus.UNKNOWN
    assert provider.get_idle_pattern_for_log().startswith("^\\s*in:")


def test_write_extension_root_bundled_server_gains_this_cao_address(tmp_path, monkeypatch):
    """P3-2: OMP is resolver-routed (``_write_extension_root`` calls
    ``resolve_mcp_server_config``) but was only covered through the resolver's
    own unit tests, not at the provider level."""
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    monkeypatch.setenv("CAO_API_PORT", "9899")
    monkeypatch.delenv("CAO_API_HOST", raising=False)
    monkeypatch.delenv("CAO_HOME_DIR", raising=False)
    provider = make_provider()
    extension_dir = provider._write_extension_root(
        {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}}
    )
    manifest = json.loads((Path(extension_dir) / ".mcp.json").read_text())
    assert manifest["mcpServers"]["cao-mcp-server"]["env"]["CAO_API_PORT"] == "9899"


def test_write_extension_root_default_cao_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    for key in ("CAO_API_HOST", "CAO_API_PORT", "CAO_HOME_DIR"):
        monkeypatch.delenv(key, raising=False)
    provider = make_provider()
    extension_dir = provider._write_extension_root(
        {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}}
    )
    manifest = json.loads((Path(extension_dir) / ".mcp.json").read_text())
    assert manifest["mcpServers"]["cao-mcp-server"]["env"] == {
        "CAO_TERMINAL_ID": provider.terminal_id
    }


def test_extracts_unterminated_final_block_and_cleanup_tolerates_removal_errors(tmp_path):
    provider = make_provider()
    assert (
        provider.extract_last_message_from_script(
            "\x1b[48;2;5;5;5m\n request\n\n\x1b[49m answer\n╰─ frame ─╯\n"
        )
        == "answer"
    )

    provider._artifact_dir = tmp_path / "generated"
    with patch(
        "cli_agent_orchestrator.providers.omp.shutil.rmtree",
        side_effect=[FileNotFoundError, OSError("locked")],
    ):
        provider.cleanup()
        provider._artifact_dir = tmp_path / "generated"
        provider.cleanup()

    assert provider._artifact_dir is None


def test_the_profile_load_is_wrapped_in_plugin_delivery(tmp_path, monkeypatch):
    """Reproduced by review 3 on #584: OMP never saw installed plugins' MCP servers.

    OMP has a native ``profile.mcpServers`` serializer (``_write_extension_root``)
    and re-reads the profile at launch, exactly like the providers the previous
    review wired — but its ``_load_profile`` was not wrapped, so plugin delivery
    stopped at the install path and never reached the extension root.

    The provider key is asserted too, not just the call: the key selects the
    ``PROVIDER_TRANSPORTS`` row, so a wrong one silently falls through to the
    stdio-only default.
    """
    from cli_agent_orchestrator.models.provider import ProviderType

    calls = []

    def spy(profile, provider=None):
        calls.append((profile, provider))
        return profile

    monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr("cli_agent_orchestrator.providers.omp._with_plugin_mcp", spy)
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.omp.load_agent_profile", lambda _name: make_profile()
    )

    _command_parts(make_provider(agent_profile="analyst"))

    assert calls, "omp built its command without passing the profile through plugin delivery"
    assert all(provider_key == ProviderType.OMP.value for _, provider_key in calls), calls
