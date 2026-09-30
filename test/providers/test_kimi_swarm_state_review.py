"""Swarm completion must survive viewport eviction without accepting stale turns."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import kimi_cli as kimi_module
from cli_agent_orchestrator.providers import kimi_transcript as kt
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

ECHO = " \x1b[38;5;222m✨ Run this batch.\x1b[39m\n\n"
FINAL = " \x1b[38;5;253m● \x1b[39mThe main answer.\n"
THINKING = " \x1b[38;5;244m● Composing the main answer.\x1b[39m\n"
FOOTER = "\n\x1b[38;5;244mcontext: 1% (34/64k)\x1b[39m\n"
TOOL = " \x1b[38;5;253m● \x1b[1m\x1b[38;5;111mUsed Read\x1b[0m (a.txt) · 10 lines\n"


def panel(state="Working…"):
    return (
        "  \x1b[38;5;111m─ \x1b[1mAgent Swarm\x1b[0m"
        "\x1b[38;5;111m ─ \x1b[38;5;253mBatch\x1b[38;5;111m ─────\x1b[39m\n\n"
        "  \x1b[38;5;111m001\x1b[39m \x1b[38;5;242m["
        "\x1b[38;5;114m⣀\x1b[38;5;244m⣀⣀⣀⣀⣀⣀⣀\x1b[38;5;242m]"
        "\x1b[39m \x1b[38;5;244mitem-0\x1b[39m\n\n"
        f"  \x1b[38;5;111m{state}\x1b[39m  "
        "\x1b[38;5;111m━━━━━━━━\x1b[38;5;242m━━━━\x1b[39m\n"
    )


@pytest.fixture
def provider():
    instance = KimiCliProvider("review-swarm", "session", "window")
    instance.restore_runtime_variant("code")
    return instance


@pytest.fixture
def backend(provider, monkeypatch):
    instance = MagicMock()
    instance.supports_event_inbox.return_value = False
    instance.get_native_status.return_value = None
    instance.get_history.return_value = FOOTER
    monkeypatch.setattr(kimi_module, "get_backend", lambda: instance)
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: instance)
    manager = MagicMock()
    manager.get_provider.return_value = provider
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.provider_manager", manager)
    return instance


def begin(provider, activity=None):
    provider.mark_input_received()
    provider._last_dispatch_time = time.time() - 20
    provider.observe_execution_output(
        activity or ECHO + panel(), provider._status_buffer_epoch, truncated=False
    )
    assert provider._execution_observed


def screen(output):
    return kt.strip_sgr(output).splitlines()


def processing_monitor(provider, output):
    monitor = StatusMonitor()
    monitor._apply_detection(provider.terminal_id, TerminalStatus.PROCESSING)
    monitor._buffers[provider.terminal_id] = output
    return monitor


@pytest.mark.parametrize("pre_eviction_observation", [False, True])
def test_long_main_answer_completes_after_its_bullet_leaves_viewport(
    provider, backend, pre_eviction_observation
):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + "Answer body.\n" * 80 + FOOTER
    backend.get_history.return_value = "\n".join(complete.splitlines()[-24:])
    if pre_eviction_observation:
        provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=True)
    assert provider.get_status(complete) is TerminalStatus.COMPLETED
    assert provider._swarm_main_answer_seen
    assert (
        provider.get_status_from_screen(screen(backend.get_history.return_value))
        is TerminalStatus.COMPLETED
    )
    monitor = processing_monitor(provider, FOOTER)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED
    monitor._apply_detection(provider.terminal_id, TerminalStatus.PROCESSING)
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.COMPLETED


@pytest.mark.parametrize("state", ["Working…", "Completed."])
@pytest.mark.parametrize("probe_failure", [False, True])
def test_restored_native_panel_without_main_answer_never_latches_ready(
    provider, backend, state, probe_failure
):
    output = ECHO + panel(state) + THINKING + FOOTER
    assert provider._last_dispatch_time == 0
    assert not provider._swarm_turn_seen
    if probe_failure:
        backend.get_history.side_effect = RuntimeError("pane unavailable")
    else:
        backend.get_history.return_value = output
    expected = TerminalStatus.UNKNOWN if probe_failure else TerminalStatus.PROCESSING
    assert provider.get_status(output) is expected
    assert provider.get_status_from_screen(screen(output)) is expected
    monitor = processing_monitor(provider, output)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.PROCESSING
    backend.get_history.side_effect = None
    complete = output + FINAL + FOOTER
    backend.get_history.return_value = complete
    monitor._buffers[provider.terminal_id] = complete
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED


@pytest.mark.parametrize("after_tool", [False, True])
def test_swarm_seen_after_execution_survives_echo_then_header_eviction(
    provider, backend, after_tool
):
    begin(provider, "⠙ Thinking…\n")
    assert not provider._swarm_turn_seen
    output = (TOOL + "  \x1b[2mpayload\x1b[0m\n" if after_tool else "") + panel() + FOOTER
    provider.observe_execution_output(output, provider._status_buffer_epoch, truncated=True)
    assert provider._swarm_turn_seen
    backend.get_history.side_effect = RuntimeError("pane unavailable")
    assert provider.get_status(output) is TerminalStatus.UNKNOWN
    assert provider.get_status(FOOTER) is TerminalStatus.UNKNOWN
    assert provider.get_status_from_screen(screen(FOOTER)) is TerminalStatus.UNKNOWN
    monitor = processing_monitor(provider, FOOTER)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.PROCESSING


@pytest.mark.parametrize("private_prefix", ["", TOOL, "```text\n"])
def test_echo_evicted_before_initial_execution_cannot_accept_panel(provider, private_prefix):
    provider.mark_input_received()
    provider.observe_execution_output(
        private_prefix + panel() + FOOTER, provider._status_buffer_epoch, truncated=True
    )
    assert not provider._execution_observed
    assert not provider._swarm_turn_seen
    assert provider.execution_evidence_ambiguous
    assert not provider.has_execution_evidence(ECHO + panel())


@pytest.mark.parametrize("fence", ["```text\n", "> ```text\n"])
def test_quoted_panel_cannot_become_swarm_after_ordinary_execution(provider, fence):
    begin(provider, "⠙ Thinking…\n")
    quote = panel() if not fence.startswith(">") else "> " + panel().replace("\n", "\n> ")
    provider.observe_execution_output(
        ECHO + fence + quote, provider._status_buffer_epoch, truncated=False
    )
    assert not provider._swarm_turn_seen
    assert not provider._swarm_main_answer_seen


@pytest.mark.parametrize(
    "private_answer",
    [
        "```text\n" + FINAL + "```\n",
        "> " + FINAL,
        TOOL + "  \x1b[2m● A private tool result.\x1b[0m\n",
    ],
)
def test_private_or_quoted_answer_cannot_certify_evicted_panel(provider, backend, private_answer):
    begin(provider)
    output = ECHO + panel("Completed.") + private_answer + FOOTER
    provider.observe_execution_output(output, provider._status_buffer_epoch, truncated=False)
    assert not provider._swarm_main_answer_seen
    assert provider.get_status(output) is TerminalStatus.UNKNOWN
    assert provider.get_status_from_screen(screen(FOOTER)) is TerminalStatus.UNKNOWN
    monitor = processing_monitor(provider, FOOTER)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING


@pytest.mark.parametrize("prior_tool", ["", TOOL])
def test_final_fence_opener_is_valid_main_answer_evidence(provider, backend, prior_tool):
    begin(provider)
    complete = (
        ECHO
        + prior_tool
        + panel("Completed.")
        + " \x1b[38;5;253m● ```python\x1b[39m\n"
        + "print('answer')\n```\n"
        + FOOTER
    )
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_main_answer_seen
    assert provider.get_status(FOOTER) is TerminalStatus.COMPLETED


def test_later_panel_overrules_cached_main_answer(provider, backend):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_main_answer_seen
    active = complete + panel() + FOOTER
    backend.get_history.return_value = active
    assert provider.get_status(active) is TerminalStatus.PROCESSING
    assert not provider._swarm_main_answer_seen
    backend.get_history.return_value = FOOTER
    assert provider.get_status(FOOTER) is TerminalStatus.UNKNOWN


def test_completed_panel_without_visible_main_cannot_override_current_pending_evidence(
    provider, backend
):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    backend.get_history.return_value = panel("Completed.") + FOOTER
    assert provider.get_status(complete) is TerminalStatus.PROCESSING
    assert (
        provider.get_status_from_screen(screen(backend.get_history.return_value))
        is TerminalStatus.PROCESSING
    )
    assert provider._swarm_main_answer_seen


def test_newer_panel_keeps_old_proof_invalid_until_fresh_main(provider, backend):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    monitor = processing_monitor(provider, complete)
    backend.get_history.return_value = TOOL + panel() + FOOTER
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    floor = provider._swarm_final_floor
    assert floor > 0
    backend.get_history.return_value = TOOL + panel("Completed.") + THINKING + FOOTER
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert not provider._swarm_main_answer_seen
    provider.record_status_chunk(THINKING + FOOTER, provider._status_buffer_epoch)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert provider._swarm_final_floor >= floor
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert not provider._swarm_main_answer_seen
    provider.record_status_chunk(
        panel("Completed.") + FINAL + FOOTER, provider._status_buffer_epoch
    )
    backend.get_history.return_value = FINAL + FOOTER
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_complete_quote_context_excludes_active_looking_viewport_suffix(provider, backend, fence):
    begin(provider)
    suffix = panel() + fence + "\n" + FOOTER
    complete = (
        ECHO
        + panel("Completed.")
        + " \x1b[38;5;253m● "
        + fence
        + "text\x1b[39m\n"
        + "Quoted body.\n" * 80
        + suffix
    )
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    backend.get_history.return_value = suffix
    assert kt.swarm_turn_pending(suffix)
    assert kt.swarm_pane_is_quoted_suffix(suffix, provider._swarm_proof_output(""))
    assert provider.get_status(complete) is TerminalStatus.COMPLETED
    assert provider.get_status_from_screen(screen(suffix)) is TerminalStatus.COMPLETED
    assert provider._swarm_final_floor == 0


def test_pane_invalidation_blocks_inflight_old_proof(provider, backend, monkeypatch):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    read = threading.Event()
    resume = threading.Event()
    original = provider._swarm_proof_output

    def delayed_proof(output):
        proof = original(output)
        if not read.is_set():
            read.set()
            assert resume.wait(5)
        return proof

    monkeypatch.setattr(provider, "_swarm_proof_output", delayed_proof)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider._observe_swarm_turn_state, complete)
        assert read.wait(5)
        assert provider._swarm_pane_pending(TOOL + panel() + FOOTER)
        resume.set()
        assert future.result(timeout=5) is None
    assert provider._swarm_final_floor > 0
    assert not provider._swarm_main_answer_seen


def test_pane_captured_before_new_stream_revision_cannot_invalidate_fresh_main(provider, backend):
    begin(provider)
    provider.record_status_chunk(ECHO + panel(), provider._status_buffer_epoch)
    revision = provider._swarm_revision
    complete = panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_main_answer_seen
    assert provider._swarm_pane_pending(panel(), provider._swarm_generation, revision) is None
    assert provider._swarm_main_answer_seen
    assert provider._swarm_final_floor == 0


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_final_floor_uses_parser_lf_rows_with_unicode_separators(provider, backend, separator):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + ("A" + separator) * 19 + "B\n" + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    backend.get_history.return_value = TOOL + panel() + FOOTER
    assert provider.get_status(FOOTER) is TerminalStatus.PROCESSING
    proof = provider._swarm_proof_output("")
    assert provider._swarm_final_floor == len(proof.split("\n"))
    fresh = panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(fresh, provider._status_buffer_epoch)
    provider.observe_execution_output(fresh, provider._status_buffer_epoch, truncated=False)
    backend.get_history.return_value = FINAL + FOOTER
    assert provider.get_status(FOOTER) is TerminalStatus.COMPLETED
    assert provider.get_status_from_screen(screen(FINAL + FOOTER)) is TerminalStatus.COMPLETED


def test_later_batch_revokes_monitor_ready_latch_without_a_new_dispatch(provider, backend):
    begin(provider)
    monitor = processing_monitor(provider, "")
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    backend.get_history.return_value = complete
    monitor._process_chunk(provider.terminal_id, complete)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED
    assert not monitor._allow_processing_revert.get(provider.terminal_id, False)
    later = TOOL + panel() + THINKING + FOOTER
    backend.get_history.return_value = later
    monitor._process_chunk(provider.terminal_id, later)
    assert provider.has_pending_native_swarm
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.PROCESSING
    final = panel("Completed.") + FINAL + FOOTER
    backend.get_history.return_value = final
    monitor._process_chunk(provider.terminal_id, final)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED


def test_quoted_activity_after_ready_does_not_revoke_monitor_latch(provider, backend):
    begin(provider)
    monitor = processing_monitor(provider, "")
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    backend.get_history.return_value = complete
    monitor._process_chunk(provider.terminal_id, complete)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED
    quote = "```text\n" + panel() + "```\n" + FOOTER
    backend.get_history.return_value = FOOTER
    monitor._process_chunk(provider.terminal_id, quote)
    assert not provider.has_pending_native_swarm
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED


def test_pending_swarm_cannot_revoke_terminal_error_without_new_input(provider, backend):
    begin(provider)
    monitor = processing_monitor(provider, "")
    active = ECHO + panel() + FOOTER
    backend.get_history.return_value = active
    monitor._process_chunk(provider.terminal_id, active)
    assert provider.has_pending_native_swarm
    monitor._apply_detection(provider.terminal_id, TerminalStatus.ERROR)
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.ERROR
    monitor._apply_detection(provider.terminal_id, TerminalStatus.PROCESSING)
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.ERROR
    backend.get_history.return_value = FOOTER
    monitor._process_chunk(provider.terminal_id, FOOTER)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.ERROR
    monitor.notify_input_sent(provider.terminal_id)
    monitor._apply_detection(provider.terminal_id, TerminalStatus.PROCESSING)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING


def test_distinct_terminal_only_batch_after_main_answer_still_needs_new_main_answer(
    provider, backend
):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    later = complete + panel("Completed.").replace("Batch", "New batch") + FOOTER
    provider.observe_execution_output(later, provider._status_buffer_epoch, truncated=False)
    backend.get_history.return_value = later
    assert provider.get_status(later) is TerminalStatus.PROCESSING
    assert not provider._swarm_main_answer_seen


@pytest.mark.parametrize("private_prefix", ["```text\n", TOOL + "```text\n"])
def test_evicted_fence_context_does_not_turn_quoted_final_into_completion(
    provider, backend, monkeypatch, private_prefix
):
    settings = lambda: {"state_buffer_max": 1024}
    monkeypatch.setattr(kimi_module, "get_server_settings", settings)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_server_settings", settings
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", False)
    provider.mark_input_received()
    provider._last_dispatch_time = time.time() - 20
    monitor = processing_monitor(provider, "")
    history = ""

    def capture(*args, **kwargs):
        return FOOTER if kwargs.get("visible_only") else history

    backend.get_history.side_effect = capture

    for chunk in (
        ECHO + panel(),
        panel("Completed.") + private_prefix + "quoted body\n" * 200,
        FINAL + "quoted continuation\n" * 100 + FOOTER,
    ):
        history += chunk
        monitor._process_chunk(provider.terminal_id, chunk)
    assert "```" not in monitor._buffers[provider.terminal_id]
    assert provider._swarm_turn_seen
    assert not provider._swarm_main_answer_seen
    assert provider.get_status(FOOTER) is TerminalStatus.UNKNOWN
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING

    final_chunk = "```\n" + FINAL + "public answer\n" * 100 + FOOTER
    history += final_chunk
    monitor._process_chunk(provider.terminal_id, final_chunk)
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED


@pytest.mark.parametrize("reset", ["dispatch", "epoch", "cleanup"])
def test_new_generation_cannot_reuse_swarm_or_main_answer_evidence(
    provider, backend, monkeypatch, reset
):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_turn_seen and provider._swarm_main_answer_seen
    old_epoch = provider._status_buffer_epoch
    if reset == "dispatch":
        provider.mark_input_received()
    elif reset == "epoch":
        provider.notify_status_buffer_reset(old_epoch + 1)
    else:
        monkeypatch.setattr(provider, "_remove_managed_scratch", lambda: True)
        monkeypatch.setattr(provider, "_remove_managed_runtime_home", lambda: True)
        assert provider.cleanup()
    assert not provider._swarm_turn_seen
    assert not provider._swarm_main_answer_seen
    assert not provider._execution_observed
    if reset == "epoch":
        provider.observe_execution_output(complete, old_epoch, truncated=False)
        assert not provider._swarm_turn_seen
        assert not provider._swarm_main_answer_seen
    if reset != "cleanup":
        assert provider.get_status_from_screen(screen(complete)) is TerminalStatus.PROCESSING


def test_historical_final_before_latest_echo_or_panel_cannot_complete(provider, backend):
    begin(provider, "⠙ Thinking…\n")
    current = ECHO + panel("Completed.") + THINKING + FOOTER
    output = ECHO + panel("Completed.") + FINAL + current
    provider.observe_execution_output(output, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_turn_seen
    assert not provider._swarm_main_answer_seen
    backend.get_history.return_value = current
    assert provider.get_status(output) is TerminalStatus.PROCESSING


def test_restored_ordinary_answer_preserves_output_inference(provider, backend):
    complete = ECHO + FINAL + FOOTER
    assert provider.get_status(complete) is TerminalStatus.COMPLETED
    assert provider.get_status_from_screen(screen(complete)) is TerminalStatus.COMPLETED
    backend.get_history.assert_not_called()


def test_restored_seen_swarm_can_finish_with_main_answer_outside_viewport(provider, backend):
    active = ECHO + panel() + THINKING + FOOTER
    backend.get_history.return_value = active
    assert provider.get_status(active) is TerminalStatus.PROCESSING
    complete = ECHO + panel("Completed.") + FINAL + "Answer body.\n" * 80 + FOOTER
    backend.get_history.return_value = "\n".join(complete.splitlines()[-24:])
    assert not provider._execution_observed
    assert provider.get_status(complete) is TerminalStatus.COMPLETED
    assert provider._swarm_main_answer_seen
    assert provider.get_status_from_screen(screen(FOOTER)) is TerminalStatus.COMPLETED


@pytest.mark.parametrize("fence", ["```", "````", "~~~", "~~~~"])
def test_exact_stream_preserves_quoted_final_across_escape_and_buffer_cuts(
    provider, backend, monkeypatch, fence
):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_server_settings",
        lambda: {"state_buffer_max": 1024},
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", False)
    provider.mark_input_received()
    provider._last_dispatch_time = time.time() - 20
    monitor = processing_monitor(provider, "")
    chunks = [
        ECHO + panel(),
        panel("Completed.") + fence + "text\n" + "quoted row\n" * 8000,
        " \x1b[38;5",
        ";253m● Not the main answer.\x1b[39m\n" + FOOTER,
    ]
    for chunk in chunks:
        monitor._process_chunk(provider.terminal_id, chunk)
    assert provider._swarm_stream is not None
    assert provider._swarm_stream._rolled
    assert not provider._swarm_main_answer_seen
    assert provider.get_status(FOOTER) is TerminalStatus.UNKNOWN
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    monitor._process_chunk(provider.terminal_id, fence + "\n" + FINAL + FOOTER)
    assert provider._swarm_main_answer_seen
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED
    stream = provider._swarm_stream
    provider.notify_status_buffer_reset(provider._status_buffer_epoch + 1)
    assert stream.closed
    assert provider._swarm_stream is None
    assert not provider._swarm_main_answer_seen


def test_missing_context_and_unstyled_echo_cannot_certify_completion(provider, backend):
    begin(provider)
    provider.observe_execution_output(
        ECHO + panel("Completed.") + "```text\n" + "quoted row\n" * 200,
        provider._status_buffer_epoch,
        truncated=True,
    )
    fake = "✨ A quoted echo with no renderer styling.\n\n" + FINAL + FOOTER
    provider.observe_execution_output(fake, provider._status_buffer_epoch, truncated=False)
    assert not provider._swarm_main_answer_seen
    assert provider.get_status(fake) is TerminalStatus.UNKNOWN


def test_stream_storage_failure_does_not_promote_a_cropped_answer(provider, backend, monkeypatch):
    begin(provider)
    monkeypatch.setattr(
        kimi_module.tempfile,
        "SpooledTemporaryFile",
        MagicMock(side_effect=OSError("temporary storage unavailable")),
    )
    provider.record_status_chunk("```text\n", provider._status_buffer_epoch)
    assert provider._swarm_prefix_lost
    provider.observe_execution_output(
        FINAL + FOOTER, provider._status_buffer_epoch, truncated=False
    )
    assert not provider._swarm_main_answer_seen
    assert provider.get_status(FOOTER) is TerminalStatus.UNKNOWN


def test_later_read_and_swarm_after_answer_crop_clears_cached_completion(
    provider, backend, monkeypatch
):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_server_settings",
        lambda: {"state_buffer_max": 1024},
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", False)
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_main_answer_seen
    monitor = processing_monitor(provider, complete)
    later = "Main answer body.\n" * 100 + TOOL + panel() + FOOTER
    backend.get_history.return_value = TOOL + panel() + FOOTER
    monitor._process_chunk(provider.terminal_id, later)
    assert "The main answer" not in monitor._buffers[provider.terminal_id]
    assert not provider._swarm_main_answer_seen
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.PROCESSING
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.PROCESSING


@pytest.mark.parametrize("race", ["new_generation", "later_chunk"])
def test_slow_proof_cannot_commit_after_generation_or_stream_moves(
    provider, backend, monkeypatch, race
):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    read = threading.Event()
    resume = threading.Event()
    original = provider._swarm_proof_output

    def delayed_proof(output):
        proof = original(output)
        read.set()
        assert resume.wait(5)
        return proof

    monkeypatch.setattr(provider, "_swarm_proof_output", delayed_proof)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider._observe_swarm_turn_state, complete)
        assert read.wait(5)
        if race == "new_generation":
            provider.notify_status_buffer_reset(provider._status_buffer_epoch + 1)
            begin(provider)
            later = ECHO + panel("Completed.") + FOOTER
        else:
            later = TOOL + panel() + FOOTER
        provider.record_status_chunk(later, provider._status_buffer_epoch)
        resume.set()
        assert future.result(timeout=5) is None
    assert not provider._swarm_main_answer_seen
    backend.get_history.return_value = later
    assert provider.get_status(later) is TerminalStatus.PROCESSING


@pytest.mark.parametrize("reset", ["epoch", "cleanup"])
def test_spool_close_failure_still_finishes_reset(provider, backend, monkeypatch, reset):
    begin(provider)
    provider.record_status_chunk(ECHO + panel(), provider._status_buffer_epoch)
    stream = provider._swarm_stream
    assert stream is not None
    monkeypatch.setattr(stream, "close", MagicMock(side_effect=OSError("ENOSPC")))
    provider._swarm_main_answer_seen = True
    generation = provider._swarm_generation
    if reset == "epoch":
        provider.notify_status_buffer_reset(provider._status_buffer_epoch + 1)
    else:
        monkeypatch.setattr(provider, "_remove_managed_scratch", lambda: True)
        monkeypatch.setattr(provider, "_remove_managed_runtime_home", lambda: True)
        assert provider.cleanup()
    assert provider._swarm_generation > generation
    assert provider._swarm_stream is None
    assert not provider._swarm_stream_complete
    assert not provider._swarm_main_answer_seen
    assert not provider._execution_observed


@pytest.mark.parametrize("read_prefix", ["", TOOL])
def test_newer_active_pane_overrides_older_completed_stream(provider, backend, read_prefix):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_main_answer_seen
    backend.get_history.return_value = read_prefix + panel() + FOOTER
    assert provider.get_status(FOOTER) is TerminalStatus.PROCESSING
    assert not provider._swarm_main_answer_seen
    assert provider.get_status_from_screen(screen(FOOTER)) is TerminalStatus.PROCESSING


@pytest.mark.parametrize("operation", ["read", "write"])
def test_spool_io_failure_revokes_cached_answer_and_stays_closed(
    provider, backend, monkeypatch, operation
):
    begin(provider)
    complete = ECHO + panel("Completed.") + FINAL + FOOTER
    provider.record_status_chunk(complete, provider._status_buffer_epoch)
    provider.observe_execution_output(complete, provider._status_buffer_epoch, truncated=False)
    assert provider._swarm_main_answer_seen
    monkeypatch.setattr(provider._swarm_stream, operation, MagicMock(side_effect=OSError("ENOSPC")))
    later = TOOL + panel() + FOOTER
    if operation == "write":
        provider.record_status_chunk(later, provider._status_buffer_epoch)
    else:
        assert provider._swarm_proof_output(complete) is None
    assert provider._swarm_storage_failed
    assert not provider._swarm_main_answer_seen
    assert provider._swarm_proof_output(complete) is None
    provider.observe_execution_output(later, provider._status_buffer_epoch, truncated=True)
    backend.get_history.return_value = later
    assert provider.get_status(later) is TerminalStatus.PROCESSING
    backend.get_history.return_value = FOOTER
    assert provider.get_status(FOOTER) is TerminalStatus.UNKNOWN
    provider.notify_status_buffer_reset(provider._status_buffer_epoch + 1)
    assert not provider._swarm_storage_failed


def test_retained_sgr_context_certifies_final_when_color_prefix_was_cropped(
    provider, backend, monkeypatch
):
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_server_settings",
        lambda: {"state_buffer_max": 1024},
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.CAO_PYTE_STATUS", False)
    provider.mark_input_received()
    provider._last_dispatch_time = time.time() - 20
    monitor = processing_monitor(provider, "")
    for chunk in (
        ECHO + panel(),
        panel("Completed.") + "\x1b[38;5;253m" + "Main body.\n" * 200,
        "● The actual final answer.\x1b[39m\n" + FOOTER,
    ):
        monitor._process_chunk(provider.terminal_id, chunk)
    assert "38;5;253" not in monitor._buffers[provider.terminal_id]
    assert provider._swarm_main_answer_seen
    assert monitor.get_status(provider.terminal_id) is TerminalStatus.COMPLETED
