"""dispatch_input tells "nothing was typed" apart from "may have been typed".

inbox-delivery spec, "Delivery failures are classified": a failure in any step
before the backend's send call is ``failed`` (``InputNotSentError``); a failure
in the send call is ``uncertain`` unless the backend itself proves nothing was
written. Design D4 lists the pre-send steps; each one is driven to fail here.

``InputNotSentError`` is looked up on the module inside each test, so every test
fails on its own until the name exists instead of the file failing to collect.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends import base
from cli_agent_orchestrator.backends.base import TerminalBackendError, TerminalNotFoundError
from cli_agent_orchestrator.models.terminal import (
    ProviderExitedInputBlockedError,
    TerminalInputBlockedError,
    TerminalInputBlockedReason,
    TerminalStatus,
)
from cli_agent_orchestrator.services import terminal_service

MOD = "cli_agent_orchestrator.services.terminal_service"
META = {"tmux_session": "cao-s", "tmux_window": "w1", "provider": "claude_code"}


def _not_sent() -> type:
    return base.InputNotSentError


class _Seams:
    """The collaborators dispatch_input calls, each replaceable per test."""

    def __init__(self, backend, provider, pm, sm, mocks):
        self.backend = backend
        self.provider = provider
        self.provider_manager = pm
        self.status_monitor = sm
        self.mocks = mocks


@pytest.fixture
def seams():
    backend = MagicMock()
    provider = MagicMock()
    provider.assume_processing_on_dispatch = False
    provider.blocks_orchestrated_input_while_waiting_user_answer = False
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.3
    with (
        patch(f"{MOD}.get_terminal_metadata", return_value=META) as metadata,
        patch(f"{MOD}.terminal_input_refusal", return_value=None) as refusal,
        patch(f"{MOD}.provider_manager") as pm,
        patch(f"{MOD}.inject_memory_context", side_effect=lambda m, *_a: m) as memory,
        patch(f"{MOD}._refuse_if_provider_gone") as gone,
        patch(f"{MOD}.status_monitor") as sm,
        patch(f"{MOD}.get_backend", return_value=backend),
        patch(f"{MOD}.update_last_active") as last_active,
    ):
        pm.get_provider.return_value = provider
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.return_value = 0
        yield _Seams(
            backend,
            provider,
            pm,
            sm,
            {
                "get_terminal_metadata": metadata,
                "terminal_input_refusal": refusal,
                "inject_memory_context": memory,
                "_refuse_if_provider_gone": gone,
                "update_last_active": last_active,
            },
        )


def _assert_nothing_typed(seams: _Seams) -> None:
    assert seams.backend.send_keys.call_count == 0, "a pre-send failure must not type"
    assert seams.backend.send_special_key.call_count == 0, "a pre-send failure must not type"


def test_screen_read_timeout_before_typing_is_not_sent(seams):
    """Spec scenario "Screen read times out before typing" (incident 2026-11-17)."""
    seams.provider.mark_input_received.side_effect = TerminalBackendError(
        "herdr-core lost terminal.read: herdr-core sent nothing for 10.0s"
    )
    with pytest.raises(_not_sent(), match="terminal.read") as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert isinstance(raised.value, TerminalBackendError)
    assert isinstance(raised.value.__cause__, TerminalBackendError), "the cause is kept"
    _assert_nothing_typed(seams)


# Each step design D4 lists before send_keys, made to fail with an error that is
# none of the types dispatch_input's callers branch on.
_PRE_SEND_STEPS = {
    "metadata_lookup": lambda s: s.mocks["get_terminal_metadata"],
    "input_refusal_check": lambda s: s.mocks["terminal_input_refusal"],
    "provider_lookup": lambda s: s.provider_manager.get_provider,
    "status_check": lambda s: s.status_monitor.get_status,
    "memory_injection": lambda s: s.mocks["inject_memory_context"],
    "provider_gone_check": lambda s: s.mocks["_refuse_if_provider_gone"],
    "notify_input_sent": lambda s: s.status_monitor.notify_input_sent,
    "rolling_buffer_clear": lambda s: s.status_monitor.clear_rolling_buffer,
    "dispatch_boundary_sample": lambda s: s.status_monitor.output_generation,
    "mark_input_received": lambda s: s.provider.mark_input_received,
    "record_dispatched_message": lambda s: s.provider.record_dispatched_message,
}


@pytest.mark.parametrize("step", sorted(_PRE_SEND_STEPS))
def test_failure_in_any_pre_send_step_is_not_sent(seams, step):
    # Arrange
    cause = RuntimeError(f"{step} broke")
    _PRE_SEND_STEPS[step](seams).side_effect = cause

    # Act
    with pytest.raises(_not_sent()) as raised:
        terminal_service.dispatch_input("t1", "hello")

    # Assert
    assert raised.value.__cause__ is cause
    assert f"{step} broke" in str(raised.value), "the error text names the original failure"
    _assert_nothing_typed(seams)


@pytest.mark.parametrize("step", sorted(_PRE_SEND_STEPS))
def test_value_error_in_a_pre_send_step_is_not_sent(seams, step):
    """A ValueError before the send typed nothing, and stays a ValueError.

    POST /terminals/{id}/input maps a ValueError to 404, as it did before this
    change, so the wrapper is both an InputNotSentError and a ValueError.
    """
    # Arrange
    cause = ValueError(f"{step} rejected its input")
    _PRE_SEND_STEPS[step](seams).side_effect = cause

    # Act
    with pytest.raises(_not_sent()) as raised:
        terminal_service.dispatch_input("t1", "hello")

    # Assert
    assert raised.value.__cause__ is cause
    assert isinstance(raised.value, ValueError)
    _assert_nothing_typed(seams)


def test_redelivery_bookkeeping_failure_is_not_sent(seams):
    cause = RuntimeError("redelivery mark broke")
    seams.provider.mark_redelivery_received.side_effect = cause
    with pytest.raises(_not_sent()) as raised:
        terminal_service.dispatch_input("t1", "hello", redelivery=True)
    assert raised.value.__cause__ is cause
    _assert_nothing_typed(seams)


def test_failure_inside_send_keys_is_uncertain(seams):
    """Spec scenario "Send request lost after it was written"."""
    seams.backend.send_keys.side_effect = TerminalBackendError("herdr-core lost terminal.send")
    with pytest.raises(TerminalBackendError, match="lost terminal.send") as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert not isinstance(raised.value, _not_sent())


def test_not_sent_from_the_backend_passes_through(seams):
    """Spec scenario "Send request never written": the backend's own proof is kept."""
    original = _not_sent()("herdr-core unreachable for terminal.send")
    seams.backend.send_keys.side_effect = original
    with pytest.raises(_not_sent()) as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert raised.value is original, "not re-wrapped"


def test_failure_after_send_keys_is_not_not_sent(seams):
    """Plan note 10: the text was typed, so a later bookkeeping error is not "nothing typed"."""
    seams.mocks["update_last_active"].side_effect = RuntimeError("db locked")
    with pytest.raises(RuntimeError, match="db locked") as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert not isinstance(raised.value, _not_sent())
    assert seams.backend.send_keys.call_count == 1


def test_missing_terminal_keeps_its_value_error(seams):
    """Plan note 6: POST /terminals/{id}/input maps ValueError to 404."""
    seams.mocks["get_terminal_metadata"].return_value = None
    # A ValueError for the 404, whatever else it is: F5 lets the inbox classify
    # it as "nothing typed", so it may also be an InputNotSentError.
    with pytest.raises(ValueError, match="not found"):
        terminal_service.dispatch_input("t1", "hello")
    _assert_nothing_typed(seams)


def test_input_refusal_keeps_its_type(seams):
    refusal = TerminalInputBlockedError(
        "identity check pending", reason=TerminalInputBlockedReason.IDENTITY_PENDING
    )
    seams.mocks["terminal_input_refusal"].return_value = refusal
    with pytest.raises(TerminalInputBlockedError) as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert raised.value is refusal
    assert not isinstance(raised.value, _not_sent())


def test_provider_error_refusal_keeps_its_type(seams):
    seams.status_monitor.get_status.return_value = TerminalStatus.ERROR
    with pytest.raises(ProviderExitedInputBlockedError) as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert raised.value.reason == TerminalInputBlockedReason.PROVIDER_ERROR
    assert not isinstance(raised.value, _not_sent())
    _assert_nothing_typed(seams)


def test_bare_shell_refusal_keeps_its_type(seams):
    refusal = ProviderExitedInputBlockedError(
        "bare shell", reason=TerminalInputBlockedReason.PROVIDER_ERROR
    )
    seams.mocks["_refuse_if_provider_gone"].side_effect = refusal
    with pytest.raises(ProviderExitedInputBlockedError) as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert raised.value is refusal
    assert not isinstance(raised.value, _not_sent())
    _assert_nothing_typed(seams)


@pytest.mark.parametrize("where", ["before_send", "in_send"])
def test_unresolved_pane_keeps_terminal_not_found(seams, where):
    """inbox_service resets TerminalNotFoundError to PENDING; that branch must still see it."""
    missing = TerminalNotFoundError("cao-s:w1", "no live pane")
    if where == "before_send":
        seams.status_monitor.clear_rolling_buffer.side_effect = missing
    else:
        seams.backend.send_keys.side_effect = missing
    with pytest.raises(TerminalNotFoundError) as raised:
        terminal_service.dispatch_input("t1", "hello")
    assert raised.value is missing
    assert not isinstance(raised.value, _not_sent())


def test_not_sent_is_a_backend_error():
    """Callers that catch TerminalBackendError keep catching it."""
    assert issubclass(_not_sent(), TerminalBackendError)
